"""Real browser SSO: provider identity comes from navigation, never metadata."""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from loom_orchestration.auth import AuthProfileStore, CALLBACK, Login
from tests.contracts.test_auth_client import Observer

pw = pytest.importorskip("playwright.async_api")


@pytest.mark.parametrize("method", ["browser-cookie", "redirect", "device-code"])
@pytest.mark.parametrize("persistent_cookie", [False, True])
async def test_resources_share_actual_provider_and_follow_provider_changes(
    method, persistent_cookie, tmp_path, monkeypatch,
):
    await exercise_sso(tmp_path, monkeypatch, method, persistent_cookie)


async def test_disabling_auth_state_requires_login_for_each_resource(tmp_path, monkeypatch):
    await exercise_sso(tmp_path, monkeypatch, "browser-cookie", False, reuse=False)


async def test_rejected_service_cookie_does_not_clear_provider_or_other_resources(tmp_path, monkeypatch):
    await exercise_sso(tmp_path, monkeypatch, "browser-cookie", False, reject_saved=True)


async def exercise_sso(tmp_path, monkeypatch, method, persistent_cookie, reuse=True, reject_saved=False):
    async with pw.async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Chromium is installed with the SDK auth extra")
    observers, providers, pages, sign_ins, callbacks = {}, {}, [], [], []
    launch = pw.BrowserType.launch
    persistent = pw.BrowserType.launch_persistent_context
    new_context = pw.Browser.new_context
    new_page = pw.BrowserContext.new_page

    async def provider(route):
        url = urlsplit(route.request.url)
        service = parse_qs(url.query).get("service", [url.hostname.split(".")[0]])[0]
        observer = observers[service]
        cookies = await route.request.header_value("cookie") or ""
        async def navigate(destination, headers=None):
            # A new document navigation lets Playwright intercept each fixture
            # origin (HTTP redirect chains only route the initial request).
            await route.fulfill(content_type="text/html", headers=headers or {},
                                body="<script>location.href=" + json.dumps(destination) + "</script>")
        if url.hostname.startswith("idp"):
            assert "service_session=" not in cookies
            if url.path == "/sign-in":
                sign_ins.append(url.hostname)
                age = "; Max-Age=3600" if persistent_cookie else ""
                await navigate("/authorize?service=" + service, {
                    "Set-Cookie": "idp_session=authenticated; Path=/; Secure; HttpOnly" + age,
                })
            elif "idp_session=authenticated" not in cookies:
                await route.fulfill(content_type="text/html", body=(
                    '<button onclick="location.href=\'/sign-in?service=' + service + '\'">Sign in</button>'))
            else:
                await navigate(f"https://{service}.example/complete")
        else:
            assert "idp_session=" not in cookies
            if url.path == "/login":
                await navigate(f"https://{providers[service]}/authorize?service={service}", {
                    "Set-Cookie": "service_session=; Path=/; Secure; HttpOnly; Max-Age=0"})
            elif observer.method == "redirect":
                await navigate(CALLBACK + f"?code={service}&state=expected")
            else:
                if observer.method == "device-code":
                    observer.ready = True
                await route.fulfill(content_type="text/html", body="Signed in", headers={
                    "Set-Cookie": f"service_session={service}; Path=/; HttpOnly; Secure"})

    async def setup(context):
        await context.route("https://*.example/**", provider)
        return context

    async def headless(self, **kwargs):
        return await launch(self, **{**kwargs, "headless": True})

    async def shared_context(self, *args, **kwargs):
        return await setup(await persistent(self, *args, **{**kwargs, "headless": True}))

    async def ephemeral(self, **kwargs):
        return await setup(await new_context(self, **kwargs))

    async def record_page(self, **kwargs):
        page = await new_page(self, **kwargs)
        pages.append(page)
        return page

    monkeypatch.setattr(pw.BrowserType, "launch", headless)
    monkeypatch.setattr(pw.BrowserType, "launch_persistent_context", shared_context)
    monkeypatch.setattr(pw.Browser, "new_context", ephemeral)
    monkeypatch.setattr(pw.BrowserContext, "new_page", record_page)

    async def authenticate(service, actual_provider, legacy_idp, reject=False):
        observer = Observer("browser-cookie" if service == "first" else method)
        observers[service], providers[service] = observer, actual_provider
        observer.auth.update(cookie_name="service_session", verification_uri=f"https://{service}.example/login")
        describe = observer.describe

        async def describe_with_challenge(target):
            result = await describe(target)
            if not observer.ready:
                result["resource"]["metadata"]["authentication"]["verification_uri"] = observer.auth["verification_uri"]
            return result

        observer.describe = describe_with_challenge
        if legacy_idp:
            observer.auth["idp"] = legacy_idp

        rejected = False

        def bridge(request):
            nonlocal rejected
            body = json.loads(request.content)
            if body["action"] == "open":
                return httpx.Response(200, json={"status": "redirect", "location": observer.auth["verification_uri"],
                                                "browser_binding": service})
            assert body["browser_binding"] == service
            expected = ({"code": [service], "state": ["expected"]} if observer.method == "redirect"
                        else {"service_session": [service]})
            assert body["query"] == expected
            if reject and not rejected:
                rejected = True
                return httpx.Response(409, json={"detail": "browser_login_verification_failed"})
            callbacks.append(service)
            observer.ready = True
            return httpx.Response(200, json={"status": "accepted"})

        observer.transport = httpx.MockTransport(bridge)
        # A new store models restarting the helper between resource logins.
        store = AuthProfileStore(tmp_path / "profiles") if reuse else None
        login = Login(service, lambda: observer, profile_store=store)
        task = asyncio.create_task(login.authenticate())
        try:
            async with asyncio.timeout(15):
                while not task.done():
                    for page in list(pages):
                        if page.is_closed():
                            continue
                        try:
                            button = page.get_by_role("button", name="Sign in", exact=True)
                            if await button.count():
                                await button.click(timeout=1000)
                        except pw.Error:
                            pass  # The login task owns browser closure/errors.
                    await asyncio.sleep(.02)
                await task
            assert login.status()["phase"] == "completed"
            assert len(observer.calls) == (2 if reject else 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    await authenticate("first", "idp.example", None)
    await authenticate("second", "idp.example", "https://wrong-metadata.example")
    assert sign_ins == ["idp.example"] * (1 if reuse else 2)
    if reject_saved:
        await authenticate("first", "idp.example", None, reject=True)
        cookies = AuthProfileStore(tmp_path / "profiles").session_cookies()
        assert any(c["name"] == "service_session" and c["domain"] == "second.example" for c in cookies)
        assert sign_ins == ["idp.example"]
    # The same resource changes provider without changing its metadata. The new
    # provider must not receive the old provider's cookie.
    await authenticate("second", "idp2.example", "https://wrong-metadata.example")
    await authenticate("fourth", "idp2.example", None)
    assert sign_ins == (["idp.example", "idp2.example"] if reuse
                        else ["idp.example", "idp.example", "idp2.example", "idp2.example"])
    expected = ["first", "second"] + (["first"] if reject_saved else []) + ["second", "fourth"]
    assert callbacks == (["first"] if method == "device-code" else expected)
    if not reuse:
        assert not (tmp_path / "profiles").exists()
