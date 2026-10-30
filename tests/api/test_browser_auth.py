import time
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loom_v2.observer.browser_auth import BrowserAuth
from loom_v2.observer.authentication import authentication_view


def test_public_authentication_ignores_historical_package_idp():
    package = SimpleNamespace(body={"authentication": {
        "idp": "https://old-provider.example",
        "interaction": {"mode": "browser-cookie", "cookie_name": "sid",
                        "idp": "https://another-old-provider.example"},
    }})
    assert authentication_view(package, {"status": "authenticated", "cookie": "private"}) == {
        "method": "browser-cookie", "cookie_name": "sid", "status": "authenticated",
    }


def test_observer_bridge_requires_bearer_and_consumes_binding() -> None:
    app = FastAPI()
    auth = BrowserAuth(app, SimpleNamespace())
    session_id = "session-bridge"
    auth.sessions[session_id] = {
        "expires_at": time.time() + 60,
        "bridge_token": "token-bridge",
        "phase": "created",
    }

    async def fake_open(value):
        assert value == session_id
        auth.sessions[session_id]["phase"] = "bound"
        auth.sessions[session_id]["bound"] = "binding-bridge"
        return "https://idp.example/authorize", "binding-bridge"

    called: list[tuple[str, str, dict]] = []

    async def fake_callback(value, binding, query):
        called.append((value, binding, query))
        auth.sessions.pop(value)
        return {"status": "accepted"}

    auth._open_session = fake_open
    auth._callback_session = fake_callback
    with TestClient(app) as client:
        missing = client.post("/api/v1/auth/browser/bridge", json={
            "action": "open", "session_id": session_id,
        })
        assert missing.status_code == 400
        opened = client.post(
            "/api/v1/auth/browser/bridge",
            headers={"Authorization": "Bearer token-bridge"},
            json={"action": "open", "session_id": session_id, "query": {}},
        )
        assert opened.status_code == 200
        assert opened.json() == {
            "status": "redirect", "location": "https://idp.example/authorize",
            "browser_binding": "binding-bridge",
        }
        callback = client.post(
            "/api/v1/auth/browser/bridge",
            headers={"Authorization": "Bearer token-bridge"},
            json={"action": "callback", "session_id": session_id,
                  "browser_binding": "binding-bridge", "query": {"code": ["x"]}},
        )
        assert callback.status_code == 200
        assert called == [(session_id, "binding-bridge", {"code": ["x"]})]


@pytest.mark.parametrize("method", ["browser-cookie", "redirect"])
def test_pending_callback_keeps_only_cookie_session_until_verified(method):
    app = FastAPI()
    auth = BrowserAuth(app, SimpleNamespace())
    session_id = "s" * 32
    expiry = time.time() + 60
    auth.sessions[session_id] = {
        "expires_at": expiry, "bridge_token": "token", "phase": "bound",
        "bound": "binding", "method": method,
    }
    calls = []

    async def send(session, action, payload):
        calls.append(payload["query"])
        assert action == "callback"
        return {"status": "pending" if len(calls) == 1 else "accepted"}

    auth.send = send
    headers = {"Authorization": "Bearer token"}
    payload = {"action": "callback", "session_id": session_id,
               "browser_binding": "binding", "query": {"overleaf.sid": ["anonymous"]}}
    with TestClient(app) as client:
        pending = client.post(auth.bridge_path, headers=headers, json=payload)
        if method == "redirect":
            assert pending.status_code == 409
            assert session_id not in auth.sessions
            return
        assert pending.status_code == 200 and pending.json() == {"status": "pending"}
        assert auth.sessions[session_id]["phase"] == "bound"
        assert auth.sessions[session_id]["expires_at"] == expiry
        invalid = client.post(auth.bridge_path, headers=headers,
                              json={**payload, "browser_binding": "wrong"})
        assert invalid.status_code == 400 and len(calls) == 1
        payload["query"] = {"overleaf.sid": ["authenticated"]}
        accepted = client.post(auth.bridge_path, headers=headers, json=payload)
        assert accepted.status_code == 200 and accepted.json() == {"status": "accepted"}
        assert client.post(auth.bridge_path, headers=headers, json=payload).status_code == 404
        assert len(calls) == 2


@pytest.mark.parametrize("failure", ["expired", "provider_error", "invalid_result"])
def test_pending_cookie_session_still_expires_and_rejects_terminal_failures(failure):
    app = FastAPI()
    auth = BrowserAuth(app, SimpleNamespace())
    session_id = "s" * 32
    auth.sessions[session_id] = {
        "expires_at": time.time() + 60, "bridge_token": "token", "phase": "bound",
        "bound": "binding", "method": "browser-cookie",
    }
    calls = []

    async def send(session, action, payload):
        calls.append(action)
        if len(calls) == 1:
            return {"status": "pending"}
        if failure == "provider_error":
            raise RuntimeError("private-provider-error")
        return {"status": "pending", "cookie": "private-cookie"}

    auth.send = send
    headers = {"Authorization": "Bearer token"}
    payload = {"action": "callback", "session_id": session_id,
               "browser_binding": "binding", "query": {"overleaf.sid": ["anonymous"]}}
    with TestClient(app) as client:
        assert client.post(auth.bridge_path, headers=headers, json=payload).json() == {"status": "pending"}
        if failure == "expired":
            auth.sessions[session_id]["expires_at"] = time.time() - 1
        response = client.post(auth.bridge_path, headers=headers, json=payload)
        assert response.status_code == (404 if failure == "expired" else 409)
        assert "private" not in response.text
        assert session_id not in auth.sessions
