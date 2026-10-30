import asyncio
from http.server import ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from loom_orchestration.auth import AuthProfileStore, AutoLogin, Handler, Login
from tests.contracts.test_auth_client import Observer


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "automatic login did not finish"
        time.sleep(.01)


async def test_automatic_login_skips_ready_resource_without_invalidating_credentials():
    observer = Observer()
    observer.ready = True
    login = Login("ready", lambda: observer)
    login._automatic = True
    await login.authenticate()
    assert login.status()["phase"] == "completed"
    assert login.status()["completed_via"] == "already_ready"
    assert observer.calls == []


async def test_waiting_for_manual_browser_does_not_consume_automatic_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("loom_orchestration.auth.AUTO_ATTEMPT_SECONDS", .05)
    observer = Observer()
    observer.ready = True
    store = AuthProfileStore(tmp_path)
    login = Login("ready", lambda: observer, profile_store=store)
    login._automatic = True
    async with store.lock(threading.Event()):
        task = asyncio.create_task(login.authenticate())
        await asyncio.sleep(.15)
        assert not task.done()
        assert observer.calls == []
    await asyncio.wait_for(task, 1)
    assert login.status()["phase"] == "completed"


def test_batch_continues_and_manual_success_retries_only_unfinished(tmp_path, monkeypatch):
    store = AuthProfileStore(tmp_path)
    logins = {name: Login(name, Observer, profile_store=store) for name in ["manual", "reuse", "failure"]}
    attempts, session = [], threading.Event()

    async def authenticate(login):
        attempts.append((login.target, login._automatic))
        if login.target == "manual" and login._automatic and not session.is_set():
            raise RuntimeError("automatic_login_incomplete")
        if login.target == "failure" and not session.is_set():
            raise RuntimeError("observer_bridge_timeout")
        if not login._automatic:
            session.set()
        login._update("completed")

    monkeypatch.setattr(Login, "_authenticate", authenticate)
    automatic = AutoLogin(logins)
    try:
        assert not automatic.status()["enabled"] and not attempts
        automatic.set_enabled(True)
        wait_for(lambda: len(attempts) == 3 and not automatic.status()["running"])
        assert [v.status()["phase"] for v in logins.values()] == ["needs_manual", "completed", "failed"]
        assert logins["failure"].status()["error"] == "observer_bridge_timeout"
        logins["manual"].start()
        wait_for(lambda: logins["failure"].status()["phase"] == "completed")
        assert attempts == [("manual", True), ("reuse", True), ("failure", True), ("manual", False), ("failure", True)]
    finally:
        automatic.close()
        for login in logins.values():
            login.close()


def test_disabling_stops_automatic_batch_and_preserves_manual_login(tmp_path, monkeypatch):
    store = AuthProfileStore(tmp_path)
    logins = {name: Login(name, Observer, profile_store=store) for name in ["first", "second"]}
    entered = threading.Event()
    attempts = []

    async def authenticate(login):
        attempts.append((login.target, login._automatic))
        entered.set()
        while not login._stop.is_set():
            await asyncio.sleep(.01)
        raise RuntimeError("login_cancelled")

    monkeypatch.setattr(Login, "_authenticate", authenticate)
    automatic = AutoLogin(logins)
    try:
        automatic.set_enabled(True)
        assert entered.wait(2)
        automatic.set_enabled(False)
        wait_for(lambda: not automatic.status()["running"])
        assert attempts == [("first", True)]
        assert logins["first"].status()["phase"] == "idle"
        entered.clear()
        logins["second"].start()
        assert entered.wait(2)
        automatic.set_enabled(False)
        assert not logins["second"]._stop.is_set()
    finally:
        automatic.close()
        for login in logins.values():
            login.close()


def test_automatic_api_requires_csrf_and_boolean_and_keeps_refresh_state(tmp_path, monkeypatch):
    login = Login("resource", Observer, profile_store=AuthProfileStore(tmp_path))

    async def complete():
        login._update("completed")

    monkeypatch.setattr(login, "_authenticate", complete)
    automatic = AutoLogin({login.target: login})
    handler = type("AutoHandler", (Handler,), {"logins": automatic.logins, "automatic": automatic, "csrf": "csrf"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as http:
            assert not http.get("/auto-login").json()["enabled"]
            assert http.post("/auto-login", json={"enabled": True}).status_code == 403
            assert http.post("/auto-login", json={"enabled": True, "csrf": "csrf"},
                             headers={"Origin": "https://untrusted.example"}).status_code == 403
            assert http.post("/auto-login", json={"enabled": "true", "csrf": "csrf"}).status_code == 400
            assert http.post("/auto-login", json={"enabled": True, "csrf": "csrf"}).status_code == 202
            assert http.get("/auto-login").json()["enabled"]
            wait_for(lambda: login.status()["phase"] == "completed")
            assert http.post("/auto-login", json={"enabled": False, "csrf": "csrf"}).status_code == 202
            assert not http.get("/auto-login").json()["enabled"]
            assert "private" not in json.dumps(http.get("/status").json())
    finally:
        automatic.close()
        login.close()
        server.shutdown()
        server.server_close()


def test_automatic_login_is_unavailable_without_shared_state():
    automatic = AutoLogin({"resource": Login("resource", Observer)})
    try:
        assert not automatic.status()["available"]
        with pytest.raises(ValueError, match="automatic_login_unavailable"):
            automatic.set_enabled(True)
    finally:
        automatic.close()


def test_automatic_attempt_timeout_reports_failure_not_human_login(monkeypatch):
    monkeypatch.setattr("loom_orchestration.auth.AUTO_ATTEMPT_SECONDS", .05)
    login = Login("slow", Observer)
    login._automatic = True

    async def slow():
        await asyncio.sleep(10)

    monkeypatch.setattr(login, "_authenticate", slow)
    login._run()
    assert login.status()["phase"] == "failed"
    assert login.status()["error"] == "automatic_login_timeout"


@pytest.mark.parametrize("method", ["browser-cookie", "redirect", "device-code"])
@pytest.mark.parametrize("can_reuse", [True, False])
async def test_real_headless_browser_reuses_session_or_returns_for_manual_login(
    tmp_path, monkeypatch, method, can_reuse,
):
    from pathlib import Path
    from urllib.parse import urlsplit
    from loom_orchestration.auth import CALLBACK
    pw = pytest.importorskip("playwright.async_api")
    async with pw.async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Chromium is installed with the SDK auth extra")
    store = AuthProfileStore(tmp_path)
    if can_reuse:
        store.save_session_cookies([{"name": "identity", "value": "saved", "domain": "idp.example",
                                    "path": "/", "expires": -1, "secure": True, "httpOnly": True}])
    observer = Observer(method)
    observer.auth["verification_uri"] = "https://service.example/login"
    describe = observer.describe

    async def description(target):
        result = await describe(target)
        result["resource"]["metadata"]["authentication"]["verification_uri"] = observer.auth["verification_uri"]
        return result

    observer.describe = description
    login = Login("resource", lambda: observer, profile_store=store)
    login._automatic = True
    monkeypatch.setattr("loom_orchestration.auth.AUTO_INTERACTION_SECONDS", .2 if not can_reuse else 5)
    persistent = pw.BrowserType.launch_persistent_context
    launches = []

    async def provider(route):
        url = urlsplit(route.request.url)
        headers = {}
        if url.hostname == "idp.example":
            cookies = await route.request.header_value("cookie") or ""
            if "identity=saved" not in cookies:
                await route.fulfill(content_type="text/html", body="<h1>Sign in</h1><input type=password>")
                return
            destination = "https://service.example/complete"
        elif url.path == "/login":
            destination = "https://idp.example/authorize"
        elif method == "redirect":
            destination = CALLBACK + "?code=verified&state=expected"
        else:
            observer.ready = method == "device-code"
            await route.fulfill(content_type="text/html", body="Signed in", headers={
                "Set-Cookie": "grafana_session=verified; Secure; HttpOnly; Path=/"})
            return
        await route.fulfill(content_type="text/html", headers=headers,
                            body="<script>location.href=" + json.dumps(destination) + "</script>")

    async def context(self, *args, **kwargs):
        launches.append(kwargs["headless"])
        value = await persistent(self, *args, **kwargs)
        await value.route("https://*.example/**", provider)
        return value

    def bridge(request):
        body = json.loads(request.content)
        if body["action"] == "open":
            return httpx.Response(200, json={"status": "redirect", "location": observer.auth["verification_uri"],
                                            "browser_binding": "bound"})
        assert can_reuse
        observer.ready = True
        return httpx.Response(200, json={"status": "accepted"})

    observer.transport = httpx.MockTransport(bridge)
    monkeypatch.setattr(pw.BrowserType, "launch_persistent_context", context)
    if can_reuse:
        await login.authenticate()
        assert login.status()["phase"] == "completed"
        assert login.status()["completed_via"] == "automatic"
    else:
        with pytest.raises(RuntimeError, match="^automatic_login_incomplete$"):
            await login.authenticate()
        assert not observer.ready
    assert launches == [True]


async def test_real_portal_switch_lists_manual_and_failed_resources(tmp_path, monkeypatch):
    from pathlib import Path
    pw = pytest.importorskip("playwright.async_api")
    store = AuthProfileStore(tmp_path)
    logins = {name: Login(name, Observer, profile_store=store) for name in ["reuse", "manual", "failure"]}
    signed_in = threading.Event()

    async def authenticate(login):
        if login._automatic and not signed_in.is_set():
            if login.target == "manual":
                raise RuntimeError("automatic_login_incomplete")
            if login.target == "failure":
                raise RuntimeError("observer_bridge_timeout")
        if not login._automatic:
            signed_in.set()
        login._update("completed")

    monkeypatch.setattr(Login, "_authenticate", authenticate)
    automatic = AutoLogin(logins)
    handler = type("AutoPage", (Handler,), {"logins": logins, "automatic": automatic, "csrf": "csrf"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with pw.async_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Chromium is installed with the SDK auth extra")
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{server.server_port}/")
                switch = page.get_by_role("checkbox", name="自动复用会话登录")
                await pw.expect(switch).not_to_be_checked()
                await switch.check()
                await pw.expect(page.locator("#summary")).to_contain_text("需手动 1")
                await pw.expect(page.locator("#attention-list")).to_contain_text("manual：已有会话未能自动完成")
                await pw.expect(page.locator("#attention-list")).to_contain_text("failure：远端认证连接超时")
                await page.reload()
                await pw.expect(switch).to_be_checked()
                await page.get_by_role("button", name="手动登录", exact=True).click()
                await pw.expect(page.locator("#summary")).to_contain_text("已完成 3")
                await pw.expect(page.locator("#attention")).to_be_hidden()
                await switch.uncheck()
                await pw.expect(page.locator("#automatic-note")).to_contain_text("自动登录已关闭")
                assert errors == []
            finally:
                await browser.close()
    finally:
        automatic.close()
        for login in logins.values():
            login.close()
        server.shutdown()
        server.server_close()
