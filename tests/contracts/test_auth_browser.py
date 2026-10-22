"""Exercise the actual isolated-browser adapters against local provider fixtures."""
import json
import asyncio
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading

import httpx
import pytest

from loom_orchestration.auth import CALLBACK, Handler, Login
from tests.contracts.test_auth_client import Observer

pw = pytest.importorskip("playwright.async_api")


@pytest.mark.parametrize("method", ["redirect", "browser-cookie", "device-code"])
async def test_real_chromium_adapters_return_only_the_declared_callback(method, monkeypatch):
    async with pw.async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Chromium is installed with the SDK auth extra")
    observer = Observer(method)
    calls = []

    def bridge(request):
        value = json.loads(request.content)
        calls.append(value)
        assert request.headers["authorization"] == "Bearer private-bridge-token"
        if value["action"] == "open":
            return httpx.Response(200, json={"status": "redirect", "location": "https://login.example/authorize",
                                            "browser_binding": "private-binding"})
        observer.ready = True
        return httpx.Response(200, json={"status": "accepted"})

    observer.transport = httpx.MockTransport(bridge)
    launch = pw.BrowserType.launch
    new_context = pw.Browser.new_context

    async def headless(self, **kwargs):
        return await launch(self, **{**kwargs, "headless": True})

    async def context(self, **kwargs):
        value = await new_context(self, **kwargs)
        async def provider(route):
            if method == "redirect":
                await route.fulfill(status=302, headers={"Location": CALLBACK + "?code=private-code&state=expected-state"})
            else:
                if method == "device-code":
                    observer.ready = True
                await route.fulfill(status=200, content_type="text/html", body="<h1>Signed in</h1>")
        await value.route("https://login.example/**", provider)
        if method == "browser-cookie":
            await value.add_cookies([{"name": "grafana_session", "value": "private-cookie",
                                     "url": "https://login.example", "httpOnly": True, "secure": True}])
        return value

    monkeypatch.setattr(pw.BrowserType, "launch", headless)
    monkeypatch.setattr(pw.Browser, "new_context", context)
    login = Login(observer.names.name("activations", "test", "v1", "slave", method), lambda: observer)
    await asyncio.wait_for(login.authenticate(), timeout=15)
    assert login.status()["phase"] == "completed"
    if method == "device-code":
        assert calls == []
    else:
        assert [v["action"] for v in calls] == ["open", "callback"]
        assert calls[1]["browser_binding"] == "private-binding"
        expected = {"code": ["private-code"], "state": ["expected-state"]} if method == "redirect" else {"grafana_session": ["private-cookie"]}
        assert calls[1]["query"] == expected
    assert "private" not in json.dumps(login.status())


async def test_real_browser_renders_portal_and_retries_without_script_errors():
    login = Login("grip://loom.local/workspaces/workspace-default/activations/example/v1/slave/test", Observer)
    calls = []
    login.start = lambda: calls.append("start")
    handler = type("PortalHandler", (Handler,), {"logins": {login.target: login}, "csrf": "test-csrf"})
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
                await page.goto(f"http://127.0.0.1:{server.server_port}")
                async with page.expect_response(lambda response: response.url.endswith("/login")):
                    await page.get_by_role("button", name="登录", exact=True).click()
                login._update("expired", error="authentication_expired")
                async with page.expect_response(lambda response: response.url.endswith("/login")):
                    await page.get_by_role("button", name="重新认证").click()
                assert calls == ["start", "start"]
                assert errors == []
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
