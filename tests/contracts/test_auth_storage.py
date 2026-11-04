"""Visible browser login reads exactly one service-origin storage credential."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from loom_orchestration.auth import AuthProfileStore, Login
from tests.contracts.test_auth_client import Observer

pw = pytest.importorskip("playwright.async_api")


async def chromium_available():
    async with pw.async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Install Chromium with the SDK auth extra")


async def test_visible_manual_storage_login_then_automatic_reuse_after_helper_restart(tmp_path, monkeypatch):
    await chromium_available()
    persistent = pw.BrowserType.launch_persistent_context
    pages, launches, callbacks = [], [], []
    original_page = pw.BrowserContext.new_page
    observer = Observer("browser-storage")
    observer.auth["storage_key"] = "token"

    async def upstream(route):
        await route.fulfill(content_type="text/html", body="""
            <script>localStorage.setItem('unrelated', 'never-forward-this');</script>
            <script>if (!localStorage.getItem('token')) localStorage.setItem('token', JSON.stringify(''));</script>
            <button onclick='localStorage.setItem("token", JSON.stringify("private-service-token"));
                             this.textContent="Signed in"'>Sign in</button>
        """)

    async def launch(self, *args, **kwargs):
        launches.append(kwargs["headless"])
        context = await persistent(self, *args, **kwargs)
        await context.route("https://service.example/**", upstream)
        return context

    async def new_page(self, **kwargs):
        page = await original_page(self, **kwargs)
        pages.append(page)
        return page

    def bridge(request):
        message = json.loads(request.content)
        if message["action"] == "open":
            return httpx.Response(200, json={"status": "redirect", "location": "https://service.example/login",
                                            "browser_binding": "binding"})
        assert set(message["query"]) == {"token"}
        if message["query"] == {"token": [json.dumps("")]}:
            return httpx.Response(200, json={"status": "pending"})
        assert message["query"] == {"token": [json.dumps("private-service-token")]}
        callbacks.append(message)
        observer.ready = True
        return httpx.Response(200, json={"status": "accepted"})

    observer.transport = httpx.MockTransport(bridge)
    monkeypatch.setattr(pw.BrowserType, "launch_persistent_context", launch)
    monkeypatch.setattr(pw.BrowserContext, "new_page", new_page)
    first = Login("resource", lambda: observer, profile_store=AuthProfileStore(tmp_path))
    task = asyncio.create_task(first.authenticate())
    try:
        async with asyncio.timeout(15):
            while not pages or pages[0].url != "https://service.example/login":
                await asyncio.sleep(.02)
            await pages[0].get_by_role("button", name="Sign in", exact=True).click()
            await task
        assert first.status()["phase"] == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # A new helper instance has no in-memory credential. Chromium restores its
    # private persistent localStorage and no additional sign-in click is needed.
    observer.ready = False
    second = Login("resource", lambda: observer, profile_store=AuthProfileStore(tmp_path))
    second._automatic = True
    await asyncio.wait_for(second.authenticate(), 15)
    assert second.status()["phase"] == "completed" and second.status()["completed_via"] == "automatic"
    assert launches == [False, False] and len(callbacks) == 2
    assert "private-service-token" not in json.dumps([first.status(), second.status()])
    assert "never-forward-this" not in json.dumps(callbacks)


async def test_storage_login_ignores_identity_provider_and_clears_only_rejected_service_value(tmp_path, monkeypatch):
    await chromium_available()
    observer = Observer("browser-storage")
    observer.auth["storage_key"] = "token"
    launch = pw.BrowserType.launch
    new_context = pw.Browser.new_context
    original_page = pw.BrowserContext.new_page
    pages, candidates = [], []

    async def provider(route):
        url = urlsplit(route.request.url)
        if url.hostname == "service.example" and url.path == "/login":
            html = '<script>location.href="https://identity.example/sign-in"</script>'
        elif url.hostname == "identity.example":
            html = """<script>localStorage.setItem('token', 'private-identity-token');</script>
                <button onclick='location.href="https://service.example/complete"'>Sign in</button>"""
        else:
            html = """<script>
                localStorage.setItem('token', JSON.stringify('expired-service-token'));
                localStorage.setItem('unrelated', 'preserve-this-value');
                sessionStorage.setItem('token', 'never-read-session-storage');
                </script><button onclick='localStorage.setItem("token", JSON.stringify("fresh-service-token"))'>Finish</button>"""
        await route.fulfill(content_type="text/html", body=html)

    async def visible(self, **kwargs):
        assert kwargs["headless"] is False
        return await launch(self, **kwargs)

    async def context(self, **kwargs):
        result = await new_context(self, **kwargs)
        await result.route("https://*.example/**", provider)
        return result

    async def page(self, **kwargs):
        result = await original_page(self, **kwargs)
        pages.append(result)
        return result

    def bridge(request):
        body = json.loads(request.content)
        if body["action"] == "open":
            return httpx.Response(200, json={"status": "redirect", "location": "https://service.example/login",
                                            "browser_binding": "bound"})
        candidates.append(body["query"])
        assert set(body["query"]) == {"token"}
        value = json.loads(body["query"]["token"][0])
        assert value in {"expired-service-token", "fresh-service-token"}
        observer.ready = value == "fresh-service-token"
        return httpx.Response(200, json={"status": "accepted" if observer.ready else "pending"})

    observer.transport = httpx.MockTransport(bridge)
    monkeypatch.setattr(pw.BrowserType, "launch", visible)
    monkeypatch.setattr(pw.Browser, "new_context", context)
    monkeypatch.setattr(pw.BrowserContext, "new_page", page)
    login = Login("resource", lambda: observer)
    task = asyncio.create_task(login.authenticate())
    try:
        async with asyncio.timeout(15):
            while not pages or urlsplit(pages[0].url).hostname != "identity.example":
                await asyncio.sleep(.02)
            await pages[0].get_by_role("button", name="Sign in").wait_for()
            await asyncio.sleep(1.1)  # At least one poll while the IdP key exists.
            assert candidates == []
            await pages[0].get_by_role("button", name="Sign in").click()
            while not candidates:
                await asyncio.sleep(.02)
            while await pages[0].evaluate("localStorage.getItem('token')") is not None:
                await asyncio.sleep(.02)
            assert await pages[0].evaluate("localStorage.getItem('unrelated')") == "preserve-this-value"
            await pages[0].get_by_role("button", name="Finish").click()
            await task
        assert candidates == [{"token": [json.dumps("expired-service-token")]},
                              {"token": [json.dumps("fresh-service-token")]}]
        assert login.status()["phase"] == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
async def test_missing_display_never_falls_back_to_headless(automatic, persistent, tmp_path, monkeypatch):
    observer = Observer("browser-storage")
    observer.auth["storage_key"] = "token"
    launches = []
    def bridge(_request):
        return httpx.Response(200, json={"status": "redirect", "location": "https://service.example/login",
                                        "browser_binding": "bound"})
    observer.transport = httpx.MockTransport(bridge)
    async def unavailable(self, *args, **kwargs):
        launches.append(kwargs["headless"])
        raise pw.Error("No graphical display")
    monkeypatch.setattr(pw.BrowserType, "launch", unavailable)
    monkeypatch.setattr(pw.BrowserType, "launch_persistent_context", unavailable)
    login = Login("resource", lambda: observer,
                  profile_store=AuthProfileStore(tmp_path) if persistent else None)
    login._automatic = automatic
    with pytest.raises((pw.Error, RuntimeError)):
        await login.authenticate()
    assert launches == [False]
