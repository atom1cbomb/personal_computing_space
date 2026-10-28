"""One local login portal for declared capability authentication methods."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from .client import LoomClient


CALLBACK = "http://localhost:3000/login-callback"
METHODS = {"redirect", "browser-cookie", "device-code", "credential-profile"}
ERRORS = {"authentication_expired", "authentication_failed", "activation_changed",
          "activation_binding_unavailable",
          "login_window_closed", "login_method_unsupported", "browser_dependency_missing",
          "observer_bridge_failed", "observer_bridge_timeout", "browser_callback_failed",
          "browser_auth_open_failed", "browser_session_not_found",
          "browser_login_verification_failed", "saved_auth_rejected",
          "browser_login_location_invalid", "login_cancelled", "operator_credentials_required"}


class AuthProfileStore:
    """Keep browser authentication state in one private profile per IdP."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root is not None else None
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def profile_dir(self, idp: str | None) -> Path | None:
        if self.root is None or not isinstance(idp, str) or not idp.strip():
            return None
        value = idp.strip()
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink():
                return None
            self.root.chmod(0o700)
            path = self.root / digest
            if path.is_symlink():
                return None
            path.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            # A read-only home should not prevent a normal interactive login.
            return None
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    def has_saved_state(self, idp: str | None) -> bool:
        path = self.profile_dir(idp)
        return path is not None and (path / ".loom-auth-state").is_file()

    def mark_authenticated(self, idp: str | None) -> None:
        path = self.profile_dir(idp)
        if path is None:
            return
        marker = path / ".loom-auth-state"
        try:
            marker.touch(mode=0o600, exist_ok=True)
            marker.chmod(0o600)
        except OSError:
            pass

    def clear_saved_state(self, idp: str | None) -> None:
        path = self.profile_dir(idp)
        if path is None:
            return
        try:
            (path / ".loom-auth-state").unlink(missing_ok=True)
        except OSError:
            pass

    @contextmanager
    def lock(self, idp: str | None):
        """Serialize browser use for one persistent profile."""
        if self.root is None or not isinstance(idp, str) or not idp.strip():
            yield
            return
        key = idp.strip()
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or any(ord(c) < 32 for c in value)):
        raise RuntimeError("browser_login_location_invalid")
    return parsed.scheme + "://" + parsed.netloc


class Login:
    def __init__(self, target: str, client_factory, *, browser_executable: str | None = None,
                 profile_store: AuthProfileStore | None = None):
        self.target, self.client_factory = target, client_factory
        self.browser_executable = browser_executable
        self.profile_store = profile_store
        self.idp: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {"resource": target, "phase": "idle", "error": None}

    def _update(self, phase: str, **fields):
        with self._lock:
            self._state.update(phase=phase, **fields)

    def status(self):
        with self._lock:
            return dict(self._state)

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._state = {"resource": self.target, "phase": "starting", "error": None}
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        try:
            asyncio.run(self.authenticate())
        except Exception as exc:
            # Provider/browser errors may contain credentials or callback URLs.
            code = str(exc) if isinstance(exc, RuntimeError) else "authentication_failed"
            if code not in ERRORS:
                code = "authentication_failed"
            self._update("expired" if code == "authentication_expired" else "failed", error=code)

    def _check_deadline(self, expires_at):
        if self._stop.is_set():
            raise RuntimeError("login_cancelled")
        if time.time() >= expires_at:
            raise RuntimeError("authentication_expired")

    async def _state_from_observer(self, client, revision):
        metadata = (await client.describe(self.target))["resource"]["metadata"]
        if metadata["revision"] != revision or metadata["desired_state"] != "running":
            raise RuntimeError("activation_changed")
        authentication = metadata.get("authentication", {})
        if metadata.get("service_access", {}).get("issues"):
            raise RuntimeError("activation_binding_unavailable")
        if authentication.get("status") == "auth_failed" or metadata["state"] in {"failed", "lost", "stopped"}:
            raise RuntimeError("authentication_failed")
        return metadata["state"] == "ready" and authentication.get("status") == "authenticated", authentication

    async def _wait_ready(self, client, revision, expires_at):
        while True:
            self._check_deadline(expires_at)
            ready, _ = await self._state_from_observer(client, revision)
            if ready:
                return
            await asyncio.sleep(1)

    async def _bridge(self, client, info, action, *, binding=None, query=None):
        base, destination = urlsplit(client.base_url), urlsplit(info["bridge_url"])
        if ((base.scheme, base.netloc) != (destination.scheme, destination.netloc)
                or destination.path != "/api/v1/auth/browser/bridge" or destination.query
                or destination.fragment or destination.username or destination.password):
            raise RuntimeError("observer_bridge_failed")
        payload = {"action": action, "session_id": info["session_id"], "query": query or {}}
        if binding is not None:
            payload["browser_binding"] = binding
        async with httpx.AsyncClient(transport=client.transport, timeout=30, follow_redirects=False) as http:
            response = await http.post(info["bridge_url"], json=payload,
                                       headers={"Authorization": "Bearer " + info["session_token"]})
        if response.status_code != 200 or len(response.content) > 128 * 1024:
            if len(response.content) <= 128 * 1024:
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if detail in {"browser_callback_failed", "browser_login_verification_failed",
                              "browser_auth_open_failed", "browser_session_not_found"}:
                    raise RuntimeError(detail)
            raise RuntimeError("observer_bridge_failed")
        return response.json()

    async def _browser(self, client, auth, revision, expires_at):
        try:
            from playwright.async_api import Error as BrowserError, async_playwright
        except ImportError:
            raise RuntimeError("browser_dependency_missing") from None
        method, binding = auth["method"], None
        if method == "device-code":
            location = auth["verification_uri"]
        else:
            opened = await self._bridge(client, auth["client"], "open")
            if opened.get("status") != "redirect" or not isinstance(opened.get("browser_binding"), str):
                raise RuntimeError("observer_bridge_failed")
            location, binding = opened["location"], opened["browser_binding"]
        target_origin = https_origin(location)
        callback = None

        def capture(url):
            nonlocal callback
            parsed = urlsplit(url)
            if parsed.scheme + "://" + parsed.netloc + parsed.path != CALLBACK or len(parsed.query) > 16384:
                return False
            callback = parse_qs(parsed.query, keep_blank_values=True)
            return True

        async def intercept(route):
            if not capture(route.request.url):
                await route.abort()
                return
            await route.fulfill(status=200, content_type="text/plain; charset=utf-8",
                                body="登录信息已收到，正在验证。")

        idp = auth.get("idp") or self.idp
        profile_dir = self.profile_store.profile_dir(idp) if self.profile_store else None
        saved_profile = self.profile_store.has_saved_state(idp) if self.profile_store else False
        profile_lock = self.profile_store.lock(idp) if self.profile_store else nullcontext()
        with profile_lock:
            async with async_playwright() as playwright:
                if profile_dir is not None:
                    self._update("restoring_session")
                browser = None
                if profile_dir is not None:
                    try:
                        context = await playwright.chromium.launch_persistent_context(
                            str(profile_dir), headless=False, executable_path=self.browser_executable,
                            viewport={"width": 1120, "height": 840}, args=["--disable-dev-shm-usage"])
                    except (BrowserError, OSError):
                        # A damaged or locked profile must not prevent a normal
                        # interactive login. The next successful login starts
                        # with a clean ephemeral context.
                        profile_dir = None
                if profile_dir is None:
                    browser = await playwright.chromium.launch(headless=False, executable_path=self.browser_executable,
                                                               args=["--disable-dev-shm-usage"])
                    context = await browser.new_context(viewport={"width": 1120, "height": 840})
                try:
                    saved_cookie_attempt = False
                    if method == "browser-cookie" and profile_dir is not None:
                        saved_values = [c["value"] for c in await context.cookies(target_origin)
                                        if c["name"] == auth["cookie_name"] and c["value"]]
                        saved_cookie_attempt = len(saved_values) == 1 and len(saved_values[0]) <= 4096
                    if method == "redirect":
                        await context.route(CALLBACK + "*", intercept)
                    page = await context.new_page()
                    if method == "redirect":
                        # Playwright routes only the first URL in an HTTP redirect
                        # chain. Request events also observe the final OAuth callback.
                        page.on("request", lambda request: capture(request.url)
                                if request.is_navigation_request() and request.frame == page.main_frame else None)
                    try:
                        await page.goto(location, wait_until="domcontentloaded", timeout=30000)
                    except BrowserError:
                        if callback is None:
                            raise
                    self._update("awaiting_login")
                    while True:
                        self._check_deadline(expires_at)
                        if page.is_closed():
                            raise RuntimeError("login_window_closed")
                        if method == "device-code":
                            ready, observed = await self._state_from_observer(client, revision)
                            if ready:
                                if self.profile_store:
                                    self.profile_store.mark_authenticated(idp)
                                return
                            if observed.get("status") == "auth_required" and not observed.get("verification_uri"):
                                raise RuntimeError("authentication_expired")
                        elif method == "browser-cookie":
                            parsed = urlsplit(page.url)
                            if parsed.scheme + "://" + parsed.netloc == target_origin:
                                values = [c["value"] for c in await context.cookies(target_origin)
                                          if c["name"] == auth["cookie_name"] and c["value"]]
                                if len(values) == 1 and len(values[0]) <= 4096:
                                    callback = {auth["cookie_name"]: values}
                        if callback is not None:
                            self._update("verifying")
                            try:
                                accepted = await self._bridge(client, auth["client"], "callback", binding=binding, query=callback)
                            except RuntimeError as exc:
                                if saved_cookie_attempt and str(exc) in {
                                    "browser_callback_failed", "browser_login_verification_failed"
                                }:
                                    await context.clear_cookies(name=auth["cookie_name"],
                                                                 domain=urlsplit(target_origin).hostname)
                                    if self.profile_store:
                                        self.profile_store.clear_saved_state(idp)
                                    raise RuntimeError("saved_auth_rejected") from None
                                if saved_profile and str(exc) in {
                                    "browser_callback_failed", "browser_login_verification_failed"
                                }:
                                    await context.clear_cookies()
                                    if self.profile_store:
                                        self.profile_store.clear_saved_state(idp)
                                    raise RuntimeError("saved_auth_rejected") from None
                                raise
                            if accepted != {"status": "accepted"}:
                                if saved_cookie_attempt or saved_profile:
                                    if saved_cookie_attempt:
                                        await context.clear_cookies(name=auth["cookie_name"],
                                                                     domain=urlsplit(target_origin).hostname)
                                    else:
                                        await context.clear_cookies()
                                    if self.profile_store:
                                        self.profile_store.clear_saved_state(idp)
                                    raise RuntimeError("saved_auth_rejected")
                                raise RuntimeError("observer_bridge_failed")
                            if self.profile_store:
                                self.profile_store.mark_authenticated(idp)
                            return
                        await asyncio.sleep(1)
                finally:
                    await context.close()
                    if browser is not None:
                        await browser.close()

    async def authenticate(self):
        async with self.client_factory() as client:
            description = await client.describe(self.target)
            metadata = description["resource"]["metadata"]
            metadata_auth = metadata.get("authentication") or {}
            self.idp = metadata_auth.get("idp") if isinstance(metadata_auth.get("idp"), str) else self.idp
            revision = metadata["revision"]
            action = client.names.action("auth-start")
            if action not in {o["action"]["uri"] for o in description["offers"]}:
                raise RuntimeError("login_method_unsupported")
            for auth_round in range(2):
                result = await client.invoke(self.target, action, {"expected_revision": revision})
                if result["resource"] != self.target or result["revision"] != revision:
                    raise RuntimeError("activation_changed")
                auth = result["authentication"]
                self.idp = auth.get("idp") if isinstance(auth.get("idp"), str) else self.idp
                if auth["method"] not in METHODS:
                    raise RuntimeError("login_method_unsupported")
                if auth["status"] == "auth_failed":
                    raise RuntimeError("authentication_failed")
                if auth["method"] == "credential-profile" and auth["status"] != "authenticated":
                    raise RuntimeError("operator_credentials_required")
                # For a provider with no challenge TTL, bound the local interaction
                # only; this is not a credential expiry time.
                expires_at = auth.get("expires_at") or time.time() + 300
                if auth["status"] != "authenticated":
                    while auth["method"] == "device-code" and not auth.get("verification_uri"):
                        self._check_deadline(expires_at)
                        ready, observed = await self._state_from_observer(client, revision)
                        if ready:
                            self._update("completed")
                            return
                        auth.update(observed)
                        await asyncio.sleep(1)
                    self._update("starting", method=auth["method"], user_code=auth.get("user_code"),
                                 expires_at=auth.get("expires_at"))
                    try:
                        await self._browser(client, auth, revision, expires_at)
                    except RuntimeError as exc:
                        if str(exc) == "saved_auth_rejected" and auth_round == 0:
                            self._update("starting", retrying_saved_session=True)
                            continue
                        raise
                self._update("verifying")
                await self._wait_ready(client, revision, expires_at)
                self._update("completed")
                return

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    logins: dict[str, Login]
    csrf: str

    def log_message(self, *_args):
        return

    def _reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(body)),
                           "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        return (host in {f"localhost:{port}", f"127.0.0.1:{port}"}
                and self.headers.get("Origin", "http://" + host) == "http://" + host)

    def do_GET(self):  # noqa: N802
        if not self._local():
            self._reply(403, b"{}")
        elif self.path == "/status":
            self._reply(200, json.dumps([x.status() for x in self.logins.values()]).encode())
        elif self.path == "/":
            self._reply(200, PAGE.replace("__CSRF__", self.csrf).encode(), "text/html; charset=utf-8")
        elif urlsplit(self.path).path == "/login-callback":
            self._reply(200, "登录信息已收到，正在验证。".encode(), "text/plain; charset=utf-8")
        else:
            self._reply(404, b"{}")

    def do_POST(self):  # noqa: N802
        if not self._local() or self.path != "/login":
            self._reply(403, b"{}")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError()
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict) or not secrets.compare_digest(str(value.get("csrf", "")), self.csrf):
                self._reply(403, b"{}")
                return
            login = self.logins[value["resource"]]
        except (ValueError, KeyError, TypeError):
            self._reply(400, b"{}")
            return
        login.start()
        self._reply(202, b'{"accepted":true}')


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Loom 登录</title>
<style>body{font:17px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;line-height:1.7}
article{border-bottom:1px solid #ddd;padding:20px 0}p{overflow-wrap:anywhere}button{font:inherit;padding:6px 18px}</style>
<h1>Loom 资源登录</h1><p>选择实例，在弹出的窗口完成登录。同一 IdP 的资源会复用本地会话；入口过期或登录失败后，可以直接重新认证。</p>
<main id="logins"></main><script>
const phases={idle:'待开始',starting:'正在发起登录',restoring_session:'正在尝试本地登录信息',awaiting_login:'请在登录窗口完成认证',verifying:'正在验证',completed:'已完成',expired:'入口已过期',failed:'登录未完成'};
const errors={activation_binding_unavailable:'节点服务入口未就绪，请联系节点管理员',authentication_expired:'请重新认证',login_window_closed:'登录窗口已关闭',browser_dependency_missing:'请安装浏览器登录组件',authentication_failed:'认证或服务检查失败，请重试',activation_changed:'实例已发生变更，请重新认证',login_method_unsupported:'当前能力包尚不支持统一登录',observer_bridge_failed:'认证连接未完成，请重试',operator_credentials_required:'此实例需要操作员更新凭据配置后重新验证'};
async function refresh(){try{const values=await(await fetch('/status',{cache:'no-store'})).json();
const root=document.getElementById('logins');root.replaceChildren();for(const x of values){
const row=document.createElement('article'),name=document.createElement('p'),state=document.createElement('p'),button=document.createElement('button');
name.textContent=x.resource;state.textContent=phases[x.phase]+(x.user_code?'；授权码：'+x.user_code:'')+(x.error?'；'+(errors[x.error]||'请重新认证'):'');
button.textContent=x.phase==='idle'?'登录':'重新认证';button.disabled=['starting','awaiting_login','verifying'].includes(x.phase);
button.onclick=async()=>{button.disabled=true;await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource:x.resource,csrf:'__CSRF__'})});await refresh()};
row.append(name,state,button);root.append(row)}}catch{}}
refresh();setInterval(refresh,1500);</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--activation", action="append", required=True)
    parser.add_argument("--authority", default="loom.local")
    parser.add_argument("--workspace", default="workspace-default")
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--browser-executable")
    parser.add_argument("--auth-state-dir", type=Path, default=Path("~/.loom/auth-profiles"),
                        help="local browser profiles, partitioned by declared IdP")
    parser.add_argument("--no-auth-state", action="store_true",
                        help="disable local browser profile reuse for this helper")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    token = args.token_file.read_text().strip()

    def client_factory():
        return LoomClient(args.observer, token=token, grip_authority=args.authority,
                          workspace_id=args.workspace, allow_insecure_grip=args.allow_http, timeout=60)

    profile_store = None if args.no_auth_state else AuthProfileStore(args.auth_state_dir)
    logins = {target: Login(target, client_factory, browser_executable=args.browser_executable,
                             profile_store=profile_store)
              for target in args.activation}
    handler = type("LoginHandler", (Handler,), {"logins": logins, "csrf": secrets.token_urlsafe(32)})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Open http://localhost:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for login in logins.values():
            login.close()


if __name__ == "__main__":
    main()
