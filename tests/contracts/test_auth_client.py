import asyncio
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from loom_orchestration.auth import AuthProfileStore, Handler, Login, https_origin
from loom_orchestration.grip import Names


class Observer:
    names = Names("loom.local", "workspace-default")
    base_url = "https://observer.example"

    def __init__(self, method="redirect"):
        self.method = method
        self.calls = []
        self.ready = False
        self.revision = 1
        self.auth = {"method": method, "status": "auth_required", "expires_at": time.time() + 240,
                     "verification_uri": "https://login.example/device", "user_code": "ABCD-EFGH",
                     "client": {"bridge_url": self.base_url + "/api/v1/auth/browser/bridge",
                                "session_id": "s" * 43, "session_token": "private-bridge-token"},
                     "cookie_name": "grafana_session"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def describe(self, target):
        return {"resource": {"uri": target, "metadata": {
            "state": "ready" if self.ready else "pending_auth", "desired_state": "running", "revision": self.revision,
            "authentication": {"method": self.method, "status": "authenticated" if self.ready else "auth_required"}}},
            "offers": [{"action": {"uri": self.names.action("auth-start")}}]}

    async def invoke(self, target, action, inputs):
        self.calls.append((target, action, inputs))
        self.ready = False
        return {"resource": target, "revision": self.revision, "authentication": deepcopy(self.auth)}


@pytest.mark.parametrize("method", ["redirect", "device-code", "browser-cookie"])
async def test_each_declared_method_uses_the_same_action_and_waits_for_ready(method, monkeypatch):
    observer = Observer(method)
    target = observer.names.name("activations", "service", "v1", "slave", "test")
    login = Login(target, lambda: observer)
    seen = []

    async def browser(client, auth, revision, expires_at):
        assert login.status()["phase"] != "completed"
        seen.append(auth["method"])
        assert revision == 1
        asyncio.get_running_loop().call_later(.01, setattr, observer, "ready", True)

    monkeypatch.setattr(login, "_browser", browser)
    await login.authenticate()
    assert seen == [method]
    assert login.status()["phase"] == "completed"
    assert observer.calls == [(target, observer.names.action("auth-start"), {"expected_revision": 1})]
    assert "private-bridge-token" not in json.dumps(login.status())


def test_failed_and_expired_attempts_restart_without_restarting_the_helper(monkeypatch):
    observer = Observer()
    target = observer.names.name("activations", "service", "v1", "slave", "test")
    login = Login(target, lambda: observer)
    attempts = []

    async def browser(client, auth, revision, expires_at):
        attempts.append(revision)
        if len(attempts) == 1:
            raise RuntimeError("authentication_expired")
        if len(attempts) == 2:
            raise RuntimeError("private-cookie=secret")
        observer.ready = True

    monkeypatch.setattr(login, "_browser", browser)
    for phase in ["expired", "failed", "completed"]:
        login.start()
        login._thread.join(timeout=3)
        assert login.status()["phase"] == phase
        assert "secret" not in json.dumps(login.status())
    assert len(observer.calls) == 3
    assert all(call[0] == target and call[2] == {"expected_revision": 1} for call in observer.calls)


async def test_rejected_saved_session_restarts_auth_for_human_fallback(monkeypatch):
    observer = Observer("browser-cookie")
    observer.auth["idp"] = "https://idp.example"
    target = observer.names.name("activations", "service", "v1", "slave", "test")
    login = Login(target, lambda: observer)
    attempts = 0

    async def browser(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("saved_auth_rejected")
        observer.ready = True

    monkeypatch.setattr(login, "_browser", browser)
    await login.authenticate()
    assert attempts == 2
    assert len(observer.calls) == 2


async def test_revision_change_prevents_reporting_a_new_activation_as_login_success(monkeypatch):
    observer = Observer()
    login = Login(observer.names.name("activations", "service", "v1", "slave", "test"), lambda: observer)

    async def browser(*args):
        observer.revision = 2
        observer.ready = True

    monkeypatch.setattr(login, "_browser", browser)
    with pytest.raises(RuntimeError, match="activation_changed"):
        await login.authenticate()


async def test_authenticated_provider_with_missing_binding_reports_node_setup_fault(monkeypatch):
    observer = Observer()
    observer.ready = True
    describe = observer.describe

    async def degraded(target):
        result = await describe(target)
        result["resource"]["metadata"].update(state="degraded", runtime_state="ready",
            service_access={"issues": [{"code": "binding_unavailable", "interaction": "request-reply", "actions": []}]})
        return result

    monkeypatch.setattr(observer, "describe", degraded)
    login = Login("resource", lambda: observer)
    with pytest.raises(RuntimeError, match="activation_binding_unavailable"):
        await login._state_from_observer(observer, 1)


async def test_bridge_keeps_credentials_in_post_body_and_enforces_observer_origin():
    observer = Observer()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "accepted"})

    observer.transport = httpx.MockTransport(handle)
    login = Login("resource", lambda: observer)
    await login._bridge(observer, observer.auth["client"], "callback", binding="private-binding",
                        query={"grafana_session": ["private-cookie"]})
    request, = calls
    assert request.headers["authorization"] == "Bearer private-bridge-token"
    assert "private-cookie" not in str(request.url)
    assert json.loads(request.content)["query"] == {"grafana_session": ["private-cookie"]}
    with pytest.raises(RuntimeError, match="observer_bridge_failed"):
        await login._bridge(observer, {**observer.auth["client"], "bridge_url": "https://untrusted.example/api/v1/auth/browser/bridge"}, "open")
    assert len(calls) == 1


@pytest.mark.parametrize("detail,expected", [
    ("browser_callback_failed", "browser_callback_failed"),
    ("browser_session_not_found", "browser_session_not_found"),
    ("private-token-do-not-display", "observer_bridge_failed"),
    ({"credential": "private-token"}, "observer_bridge_failed"),
])
async def test_bridge_reports_only_allowlisted_failure_stages(detail, expected):
    observer = Observer()
    observer.transport = httpx.MockTransport(lambda _: httpx.Response(409, json={"detail": detail}))
    login = Login("resource", lambda: observer)
    with pytest.raises(RuntimeError, match="^" + expected + "$"):
        await login._bridge(observer, observer.auth["client"], "callback")


async def test_bridge_timeout_is_distinct_and_does_not_echo_request_credentials():
    observer = Observer()

    def timeout(request):
        raise httpx.ReadTimeout("private-provider-token", request=request)

    observer.transport = httpx.MockTransport(timeout)
    with pytest.raises(RuntimeError, match="^observer_bridge_timeout$"):
        await Login("resource", lambda: observer)._bridge(observer, observer.auth["client"], "open")


@pytest.mark.parametrize("profile_available,pending", [(True, False), (True, True), (False, False)])
async def test_shared_profile_without_idp_reuses_cookie_or_reports_open_failure(tmp_path, monkeypatch, profile_available, pending):
    opened: list[Path] = []
    closed = []

    class Page:
        url = "https://service.example/"
        main_frame = object()

        def on(self, *_args):
            pass

        async def goto(self, *_args, **_kwargs):
            pass

        def is_closed(self):
            return False

    class Context:
        async def route(self, *_args):
            pass

        async def new_page(self):
            return Page()

        async def add_cookies(self, cookies):
            assert cookies == []

        async def cookies(self, _origin=None):
            return [{"name": "sid", "value": "saved-session", "expires": -1}]

        async def close(self):
            closed.append(True)

    class Chromium:
        async def launch_persistent_context(self, path, **_kwargs):
            opened.append(Path(path))
            if not profile_available:
                raise RuntimeError("private profile error")
            return Context()

        async def launch(self, **_kwargs):
            raise AssertionError("persistent context expected")

    class Playwright:
        chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    async_api = ModuleType("playwright.async_api")
    async_api.Error = RuntimeError
    async_api.async_playwright = lambda: Playwright()
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)

    login = Login("resource", lambda: None, profile_store=AuthProfileStore(tmp_path / "profiles"))
    bridges = []

    async def bridge(_client, _info, action, **_kwargs):
        bridges.append(action)
        assert not closed
        if pending and bridges == ["open", "callback"]:
            return {"status": "pending"}
        return ({"status": "redirect", "location": "https://service.example/", "browser_binding": "binding"}
                if action == "open" else {"status": "accepted"})

    login._bridge = bridge
    auth = {
        "method": "browser-cookie", "cookie_name": "sid", "client": {},
    }
    if not profile_available:
        with pytest.raises(RuntimeError, match="^browser_profile_unavailable$"):
            await login._browser(SimpleNamespace(base_url="https://observer.example"), auth, 1, time.time() + 30)
        assert bridges == ["open"] and not closed
        return
    await login._browser(SimpleNamespace(base_url="https://observer.example"), auth, 1, time.time() + 30)
    assert len(opened) == 1 and opened[0].parent == tmp_path / "profiles"
    assert bridges == ["open", "callback"] + (["callback"] if pending else [])
    assert closed == [True]


def test_portal_starts_only_selected_instance_and_rejects_cross_origin_requests(monkeypatch):
    logins = {name: Login(name, Observer) for name in ["bj", "nj", "ict-bj"]}
    started = []
    for name, login in logins.items():
        monkeypatch.setattr(login, "start", lambda name=name: started.append(name))
    handler = type("TestHandler", (Handler,), {"logins": logins, "csrf": "local-csrf"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert "重新认证" in client.get("/").text
            assert client.get("/login?resource=bj").status_code == 404
            assert client.post("/login", json={"resource": "bj"}).status_code == 403
            assert client.post("/login", json={"resource": "bj", "csrf": "local-csrf"},
                               headers={"Origin": "https://untrusted.example"}).status_code == 403
            assert client.get("/", headers={"Host": "untrusted.example"}).status_code == 403
            for _ in range(2):
                assert client.post("/login", json={"resource": "nj", "csrf": "local-csrf"}).status_code == 202
            assert started == ["nj", "nj"]
            assert [x["resource"] for x in client.get("/status").json()] == list(logins)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("location", ["http://idp.example/login", "https://user:password@idp.example/login", "https://idp.example/\nlogin"])
def test_browser_requires_a_valid_https_destination(location):
    with pytest.raises(RuntimeError, match="browser_login_location_invalid"):
        https_origin(location)


def test_auth_profile_store_uses_one_private_browser_and_preserves_session_cookies(tmp_path):
    store = AuthProfileStore(tmp_path / "profiles")
    first = store.profile_dir()
    assert first == AuthProfileStore(tmp_path / "profiles").profile_dir()
    assert first.parent == tmp_path / "profiles"
    assert oct(first.stat().st_mode & 0o777) == "0o700"
    session = {"name": "sid", "value": "private", "domain": "idp.example", "path": "/", "expires": -1}
    store.save_session_cookies([session, {**session, "expires": time.time() + 3600}])
    assert store.session_cookies() == [session]
    assert (first / ".loom-session-cookies.json").stat().st_mode & 0o777 == 0o600
    store.save_session_cookies([])
    assert store.session_cookies() == []  # Revoked/deleted cookies stay removed.


@pytest.mark.parametrize("contents", ["not JSON", "{}", "null"])
def test_damaged_session_snapshot_falls_back_to_browser_login(tmp_path, contents):
    store = AuthProfileStore(tmp_path / "profiles")
    (store.profile_dir() / ".loom-session-cookies.json").write_text(contents)
    assert store.session_cookies() == []


@pytest.mark.parametrize("cancel_queued", [False, True])
async def test_shared_browser_waits_before_describe_and_auth_start(tmp_path, monkeypatch, cancel_queued):
    store = AuthProfileStore(tmp_path)
    observers = [Observer(), Observer()]
    logins = [Login(str(i), lambda i=i: observers[i], profile_store=store) for i in range(2)]
    first_opened, release = asyncio.Event(), asyncio.Event()

    async def browser(client, *_args):
        if client is observers[0]:
            first_opened.set()
            await release.wait()
        client.ready = True

    for login in logins:
        monkeypatch.setattr(login, "_browser", browser)
    tasks = [asyncio.create_task(logins[0].authenticate())]
    try:
        await asyncio.wait_for(first_opened.wait(), 2)
        tasks.append(asyncio.create_task(logins[1].authenticate()))
        await asyncio.sleep(.15)
        assert logins[1].status()["phase"] == "waiting_for_browser"
        assert observers[1].calls == []
        observers[1].revision = 2  # Read the latest revision after the queue.
        if cancel_queued:
            logins[1].close()
            with pytest.raises(RuntimeError, match="login_cancelled"):
                await asyncio.wait_for(tasks[1], 2)
        release.set()
        await asyncio.wait_for(tasks[0], 2)
        if cancel_queued:
            assert observers[1].calls == []
        else:
            await asyncio.wait_for(tasks[1], 2)
            assert len(observers[1].calls) == 1
            assert observers[1].calls[0][2] == {"expected_revision": 2}
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["authenticated", "expired", "closed", "failed"])
async def test_anonymous_cookie_waits_in_same_browser_until_verified(outcome, tmp_path, monkeypatch):
    observer = Observer("browser-cookie")
    observer.auth.update(cookie_name="overleaf.sid", idp="https://overleaf.example")
    store = AuthProfileStore(tmp_path / "profiles")
    login = Login("resource", lambda: observer, profile_store=store)
    callback_values, opened, closed = [], [], []

    class Page:
        url = "https://overleaf.example/login"

        async def goto(self, *_args, **_kwargs):
            pass

        def is_closed(self):
            return outcome == "closed" and bool(callback_values)

    class Context:
        async def new_page(self):
            return Page()

        async def clear_cookies(self, **_kwargs):
            raise AssertionError("pending login must keep the browser's current cookies")

        async def add_cookies(self, cookies):
            assert cookies == []

        async def cookies(self, _origin=None):
            return [{"name": "overleaf.sid", "value": "anonymous" if not callback_values else "verified",
                     "domain": "overleaf.example", "path": "/", "expires": -1}]

        async def close(self):
            closed.append(True)

    class Chromium:
        async def launch_persistent_context(self, *_args, **_kwargs):
            opened.append(True)
            return Context()

    class Playwright:
        chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    api = ModuleType("playwright.async_api")
    api.Error = RuntimeError
    api.async_playwright = Playwright
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    check_deadline = login._check_deadline

    def deadline(expires_at):
        check_deadline(time.time() - 1 if outcome == "expired" and callback_values else expires_at)

    monkeypatch.setattr(login, "_check_deadline", deadline)

    def bridge(request):
        body = json.loads(request.content)
        if body["action"] == "open":
            return httpx.Response(200, json={"status": "redirect", "location": Page.url,
                                            "browser_binding": "binding"})
        assert not closed
        assert not store.session_cookies()
        callback_values.append(body["query"]["overleaf.sid"][0])
        if len(callback_values) == 1:
            return httpx.Response(200, json={"status": "pending"})
        if outcome == "failed":
            return httpx.Response(409, json={"detail": "browser_callback_failed"})
        observer.ready = True
        return httpx.Response(200, json={"status": "accepted"})

    observer.transport = httpx.MockTransport(bridge)
    if outcome == "authenticated":
        await login.authenticate()
        assert login.status()["phase"] == "completed"
        assert callback_values == ["anonymous", "verified"]
        assert store.session_cookies()[0]["value"] == "verified"
    else:
        error = {"expired": "authentication_expired", "closed": "login_window_closed",
                 "failed": "browser_callback_failed"}[outcome]
        # Exercise one browser session so a saved-profile fallback cannot mask
        # an incorrectly consumed anonymous-cookie challenge.
        with pytest.raises(RuntimeError, match=error):
            await login._browser(observer, observer.auth, 1, observer.auth["expires_at"])
    assert opened == [True] and closed == [True]
    assert "anonymous" not in json.dumps(login.status())
    assert "verified" not in json.dumps(login.status())
