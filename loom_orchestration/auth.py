"""One local login portal for declared capability authentication methods."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from .client import LoomClient


CALLBACK = "http://localhost:3000/login-callback"
AUTO_INTERACTION_SECONDS = 20
AUTO_ATTEMPT_SECONDS = 60
METHODS = {"redirect", "browser-cookie", "device-code", "credential-profile"}
ERRORS = {"authentication_expired", "authentication_failed", "activation_changed",
          "activation_binding_unavailable",
          "login_window_closed", "login_method_unsupported", "browser_dependency_missing",
          "observer_bridge_failed", "observer_bridge_timeout", "browser_callback_failed",
          "browser_auth_open_failed", "browser_session_not_found",
          "browser_login_verification_failed", "saved_auth_rejected",
          "browser_profile_unavailable",
          "automatic_login_incomplete", "automatic_login_timeout",
          "browser_login_location_invalid", "login_cancelled", "operator_credentials_required"}


class AuthProfileStore:
    """One user's browser state; Chromium enforces origin and cookie boundaries."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root is not None else None
        self._browser_lock = threading.Lock()

    def profile_dir(self) -> Path | None:
        if self.root is None:
            return None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise OSError("symlink profile root")
            self.root.chmod(0o700)
            path = self.root / "browser"
            if path.is_symlink():
                raise OSError("symlink browser profile")
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
        except OSError:
            raise RuntimeError("browser_profile_unavailable") from None
        return path

    def session_cookies(self) -> list[dict]:
        path = self.profile_dir()
        if path is None:
            return []
        try:
            source = path / ".loom-session-cookies.json"
            if source.is_symlink():
                return []
            value = json.loads(source.read_text())
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def save_session_cookies(self, cookies: list[dict]) -> None:
        path = self.profile_dir()
        if path is None:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path, delete=False) as output:
                temporary = Path(output.name)
                json.dump([c for c in cookies if c.get("expires") == -1], output)
            os.replace(temporary, path / ".loom-session-cookies.json")
        except OSError:
            raise RuntimeError("browser_profile_unavailable") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @asynccontextmanager
    async def lock(self, stop: threading.Event):
        """Wait across Login threads without blocking either asyncio loop."""
        while not self._browser_lock.acquire(blocking=False):
            if stop.is_set():
                raise RuntimeError("login_cancelled")
            await asyncio.sleep(.1)
        try:
            if stop.is_set():
                raise RuntimeError("login_cancelled")
            yield
        finally:
            self._browser_lock.release()


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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._automatic = False
        self.on_authenticated = None
        self._state = {"resource": target, "phase": "idle", "error": None}
        self._phase_started = time.monotonic()

    def _update(self, phase: str, **fields):
        with self._lock:
            if self._state["phase"] != phase:
                self._phase_started = time.monotonic()
            self._state.update(phase=phase, **fields)

    def status(self):
        with self._lock:
            return {**self._state, "elapsed_seconds": int(time.monotonic() - self._phase_started)}

    def start(self, *, automatic=False):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return None
            self._stop.clear()
            self._automatic = automatic
            self._state = {"resource": self.target, "phase": "starting", "error": None,
                           "automatic": automatic}
            self._phase_started = time.monotonic()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return self._thread

    def cancel_automatic(self):
        with self._lock:
            if self._automatic and self._thread is not None and self._thread.is_alive():
                self._stop.set()

    def _run(self):
        try:
            asyncio.run(self.authenticate())
        except Exception as exc:
            # Provider/browser errors may contain credentials or callback URLs.
            code = str(exc) if isinstance(exc, RuntimeError) else "authentication_failed"
            if code not in ERRORS:
                code = "authentication_failed"
            if self._automatic and code == "login_cancelled":
                self._update("idle", error=None)
            elif self._automatic and code == "automatic_login_incomplete":
                self._update("needs_manual", error=code)
            else:
                self._update("expired" if code == "authentication_expired" else "failed", error=code)
        else:
            if not self._automatic and self.status()["phase"] == "completed" and self.on_authenticated:
                self.on_authenticated()

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
        try:
            async with httpx.AsyncClient(transport=client.transport, timeout=30, follow_redirects=False) as http:
                response = await http.post(info["bridge_url"], json=payload,
                                           headers={"Authorization": "Bearer " + info["session_token"]})
        except httpx.TimeoutException:
            raise RuntimeError("observer_bridge_timeout") from None
        if response.status_code != 200 or len(response.content) > 128 * 1024:
            if len(response.content) <= 128 * 1024:
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if isinstance(detail, str) and detail in {"browser_callback_failed", "browser_login_verification_failed",
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
            self._update("opening_login")
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

        profile_dir = self.profile_store.profile_dir() if self.profile_store else None
        async with async_playwright() as playwright:
            self._update("restoring_session" if profile_dir is not None else "opening_browser")
            browser = None
            if profile_dir is not None:
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        str(profile_dir), headless=self._automatic, executable_path=self.browser_executable,
                        viewport={"width": 1120, "height": 840}, args=["--disable-dev-shm-usage"])
                except (BrowserError, OSError):
                    # Do not silently lose SSO by opening an empty profile.
                    raise RuntimeError("browser_profile_unavailable") from None
            else:
                browser = await playwright.chromium.launch(headless=self._automatic, executable_path=self.browser_executable,
                                                           args=["--disable-dev-shm-usage"])
                context = await browser.new_context(viewport={"width": 1120, "height": 840})

            async def discard_service_cookie():
                # Include parent-domain/path cookies returned for this origin,
                # without clearing the IdP or unrelated service sessions.
                for cookie in await context.cookies(target_origin):
                    if cookie["name"] == auth["cookie_name"]:
                        await context.clear_cookies(name=cookie["name"], domain=cookie["domain"], path=cookie["path"])

            try:
                if profile_dir is not None:
                    try:
                        await context.add_cookies(self.profile_store.session_cookies())
                    except BrowserError:
                        pass  # Malformed local snapshot: continue normal login.
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
                interaction_deadline = time.monotonic() + AUTO_INTERACTION_SECONDS
                self._update("trying_saved_session" if self._automatic else "awaiting_login")
                while True:
                    self._check_deadline(expires_at)
                    if self._automatic and callback is None and time.monotonic() >= interaction_deadline:
                        raise RuntimeError("automatic_login_incomplete")
                    if page.is_closed():
                        raise RuntimeError("login_window_closed")
                    if method == "device-code":
                        ready, observed = await self._state_from_observer(client, revision)
                        if ready:
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
                                await discard_service_cookie()
                                raise RuntimeError("saved_auth_rejected") from None
                            raise
                        if method == "browser-cookie" and accepted == {"status": "pending"}:
                            saved_cookie_attempt = False
                            callback = None
                            self._update("trying_saved_session" if self._automatic else "awaiting_login")
                            await asyncio.sleep(1)
                            continue
                        if accepted != {"status": "accepted"}:
                            raise RuntimeError("observer_bridge_failed")
                        return
                    await asyncio.sleep(1)
            finally:
                try:
                    if profile_dir is not None:
                        try:
                            cookies = await context.cookies()
                        except BrowserError:
                            pass  # A user-closed window has no readable context.
                        else:
                            self.profile_store.save_session_cookies(cookies)
                finally:
                    await context.close()
                    if browser is not None:
                        await browser.close()

    async def authenticate(self):
        async def attempt():
            if not self._automatic:
                return await self._authenticate()
            try:
                async with asyncio.timeout(AUTO_ATTEMPT_SECONDS):
                    return await self._authenticate()
            except TimeoutError:
                raise RuntimeError("automatic_login_timeout") from None

        if self.profile_store is None:
            await attempt()
        else:
            self._update("waiting_for_browser")
            async with self.profile_store.lock(self._stop):
                await attempt()

    async def _authenticate(self):
        async with self.client_factory() as client:
            description = await client.describe(self.target)
            metadata = description["resource"]["metadata"]
            revision = metadata["revision"]
            if (self._automatic and metadata["desired_state"] == "running" and metadata["state"] == "ready"
                    and metadata.get("authentication", {}).get("status") == "authenticated"
                    and not metadata.get("service_access", {}).get("issues")):
                self._update("completed", completed_via="already_ready")
                return
            action = client.names.action("auth-start")
            if action not in {o["action"]["uri"] for o in description["offers"]}:
                raise RuntimeError("login_method_unsupported")
            self._update("preparing_auth")
            for auth_round in range(2):
                if self._stop.is_set():
                    raise RuntimeError("login_cancelled")
                result = await client.invoke(self.target, action, {"expected_revision": revision})
                if result["resource"] != self.target or result["revision"] != revision:
                    raise RuntimeError("activation_changed")
                auth = result["authentication"]
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
                            self._update("completed", completed_via="automatic" if self._automatic else "manual")
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
                self._update("completed", completed_via="automatic" if self._automatic else "manual")
                return

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class AutoLogin:
    """Opt-in batches; a completed manual login enables one pending-resource retry."""

    def __init__(self, logins: dict[str, Login]):
        self.logins = logins
        self.available = bool(logins) and all(x.profile_store is not None and x.profile_store.root is not None
                                             for x in logins.values())
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._enabled = self._running = self._closed = self._scan_all = False
        for login in logins.values():
            login.on_authenticated = self._retry_pending
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def status(self):
        with self._lock:
            return {"available": self.available, "enabled": self._enabled, "running": self._running}

    def set_enabled(self, enabled: bool):
        with self._lock:
            if self._closed or (enabled and not self.available):
                raise ValueError("automatic_login_unavailable")
            self._enabled = enabled
            if enabled:
                self._scan_all = True
                self._wake.set()
            else:
                for login in self.logins.values():
                    login.cancel_automatic()

    def _retry_pending(self):
        with self._lock:
            if self._enabled and not self._closed:
                self._wake.set()

    def _work(self):
        while True:
            self._wake.wait()
            with self._lock:
                self._wake.clear()
                if self._closed:
                    return
                if not self._enabled:
                    continue
                scan_all, self._scan_all = self._scan_all, False
                self._running = True
            try:
                for login in self.logins.values():
                    with self._lock:
                        if self._closed or not self._enabled:
                            break
                        if not scan_all and login.status()["phase"] == "completed":
                            continue
                        thread = login.start(automatic=True)
                    if thread is not None:
                        thread.join()
            finally:
                with self._lock:
                    self._running = False

    def close(self):
        with self._lock:
            self._closed = True
            self._enabled = False
            for login in self.logins.values():
                login.cancel_automatic()
            self._wake.set()
        self._thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    logins: dict[str, Login]
    csrf: str
    automatic: AutoLogin | None = None

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
        elif self.path == "/auto-login":
            self._reply(200, json.dumps(self.automatic.status() if self.automatic else {
                "available": False, "enabled": False, "running": False}).encode())
        elif self.path == "/":
            self._reply(200, PAGE.replace("__CSRF__", self.csrf).encode(), "text/html; charset=utf-8")
        elif urlsplit(self.path).path == "/login-callback":
            self._reply(200, "登录信息已收到，正在验证。".encode(), "text/plain; charset=utf-8")
        else:
            self._reply(404, b"{}")

    def do_POST(self):  # noqa: N802
        if not self._local() or self.path not in {"/login", "/auto-login"}:
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
            if self.path == "/auto-login":
                if self.automatic is None or type(value.get("enabled")) is not bool:
                    raise ValueError()
                self.automatic.set_enabled(value["enabled"])
            else:
                login = self.logins[value["resource"]]
        except (ValueError, KeyError, TypeError):
            self._reply(400, b"{}")
            return
        if self.path == "/login":
            login.start()
        self._reply(202, b'{"accepted":true}')


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Loom 登录</title>
<style>body{font:17px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;line-height:1.7}
article{border-bottom:1px solid #ddd;padding:20px 0}p,li{overflow-wrap:anywhere}button{font:inherit;padding:6px 18px}
.controls{background:#f4f6f8;padding:18px;border-radius:8px}input[type=checkbox]{width:18px;height:18px;margin-right:8px}
#attention{border-left:3px solid #c87918;padding-left:16px}#summary{font-weight:600}</style>
<h1>Loom 资源登录</h1><p>选择实例，在弹出的窗口完成登录。客户端会按实际登录站点复用本地会话；入口过期或登录失败后，可以直接重新认证。</p>
<div class="controls"><label><input id="automatic" type="checkbox">自动复用会话登录</label>
<p>开启后自动尝试所有选定资源，需要扫码或授权的会列在下方。手动完成一次登录后，会继续尝试其余未完成资源。</p>
<p id="automatic-note" role="status"></p></div><p id="summary" aria-live="polite"></p>
<section id="attention" hidden><h2>需要处理的资源</h2><ul id="attention-list"></ul></section>
<main id="logins"></main><script>
const phases={waiting_for_browser:'等待其他登录完成',idle:'待开始',starting:'正在连接算力空间',preparing_auth:'正在准备远端认证',opening_login:'正在建立认证连接',opening_browser:'正在打开浏览器',restoring_session:'正在尝试本地登录信息',trying_saved_session:'正在自动复用会话',needs_manual:'需手动登录或授权',awaiting_login:'请在登录窗口完成认证',verifying:'正在验证',completed:'已完成',expired:'入口已过期',failed:'登录未完成'};
const errors={automatic_login_incomplete:'已有会话未能自动完成，请手动登录或授权',automatic_login_timeout:'自动认证或验证超时，请检查连接后重试',browser_profile_unavailable:'无法打开或保存登录档案；请关闭使用同一档案的其他助手，或检查目录权限',activation_binding_unavailable:'节点服务入口未就绪，请联系节点管理员',authentication_expired:'请重新认证',login_window_closed:'登录窗口已关闭',browser_dependency_missing:'请安装浏览器登录组件',authentication_failed:'认证或服务检查失败，请重试',activation_changed:'实例已发生变更，请重新认证',login_method_unsupported:'当前能力包尚不支持统一登录',observer_bridge_failed:'认证连接未完成，请重试',observer_bridge_timeout:'远端认证连接超时，请重试',browser_auth_open_failed:'远端登录入口未能打开，请重试',browser_callback_failed:'远端未接受认证回调，请重试',browser_session_not_found:'认证会话已失效，请重新认证',operator_credentials_required:'此实例需要操作员更新凭据配置后重新验证'};
const busy=['waiting_for_browser','starting','preparing_auth','opening_login','opening_browser','restoring_session','trying_saved_session','awaiting_login','verifying'];
const autoSwitch=document.getElementById('automatic'),note=document.getElementById('automatic-note');let changing=false;
autoSwitch.onchange=async()=>{changing=true;autoSwitch.disabled=true;try{
const response=await fetch('/auto-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:autoSwitch.checked,csrf:'__CSRF__'})});
if(!response.ok)throw new Error();}catch{note.textContent='未能切换自动登录，请刷新后重试'}finally{changing=false;await refresh()}};
async function refresh(){try{const [values,automatic]=await Promise.all(['/status','/auto-login'].map(async path=>(await fetch(path,{cache:'no-store'})).json()));
if(!changing){autoSwitch.checked=automatic.enabled;autoSwitch.disabled=!automatic.available;
note.textContent=!automatic.available?'启用本地会话保存后可使用自动登录':automatic.running?(automatic.enabled?'正在依次尝试，未完成的资源会列在下方':'正在停止自动尝试'):(automatic.enabled?'自动尝试已完成；手动登录成功后会继续尝试未完成资源':'自动登录已关闭，可逐项登录');}
const count=phase=>values.filter(x=>x.phase===phase).length;
document.getElementById('summary').textContent=`已完成 ${count('completed')} · 需手动 ${count('needs_manual')} · 失败或过期 ${count('failed')+count('expired')} · 进行中 ${values.filter(x=>busy.includes(x.phase)).length} · 待开始 ${count('idle')}`;
const pending=values.filter(x=>['needs_manual','failed','expired'].includes(x.phase)),list=document.getElementById('attention-list');list.replaceChildren();
document.getElementById('attention').hidden=pending.length===0;for(const x of pending){const item=document.createElement('li');item.textContent=x.resource+'：'+(errors[x.error]||phases[x.phase]);list.append(item)}
const root=document.getElementById('logins');root.replaceChildren();for(const x of values){
const row=document.createElement('article'),name=document.createElement('p'),state=document.createElement('p'),button=document.createElement('button');
name.textContent=x.resource;state.textContent=(x.completed_via==='already_ready'?'已就绪，无需重新登录':phases[x.phase])+(x.user_code?'；授权码：'+x.user_code:'')+(x.error?'；'+(errors[x.error]||'请重新认证'):'')+(busy.includes(x.phase)?'；已等待 '+x.elapsed_seconds+' 秒':'');
button.textContent=x.phase==='needs_manual'?'手动登录':x.phase==='idle'?'登录':'重新认证';button.disabled=busy.includes(x.phase);
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
                        help="private shared browser profile for this user; use a separate directory for another account")
    parser.add_argument("--no-auth-state", action="store_true",
                        help="disable local browser profile reuse for this helper")
    parser.add_argument("--auto-login", action="store_true",
                        help="automatically try saved sessions for all selected resources")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if args.auto_login and args.no_auth_state:
        parser.error("--auto-login requires local auth state; remove --no-auth-state")
    token = args.token_file.read_text().strip()

    def client_factory():
        return LoomClient(args.observer, token=token, grip_authority=args.authority,
                          workspace_id=args.workspace, allow_insecure_grip=args.allow_http, timeout=60)

    profile_store = None if args.no_auth_state else AuthProfileStore(args.auth_state_dir)
    logins = {target: Login(target, client_factory, browser_executable=args.browser_executable,
                             profile_store=profile_store)
              for target in args.activation}
    automatic = AutoLogin(logins)
    handler = type("LoginHandler", (Handler,), {"logins": logins, "automatic": automatic,
                                               "csrf": secrets.token_urlsafe(32)})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Open http://localhost:{args.port}/", flush=True)
    try:
        if args.auto_login:
            automatic.set_enabled(True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        automatic.close()
        for login in logins.values():
            login.close()


if __name__ == "__main__":
    main()
