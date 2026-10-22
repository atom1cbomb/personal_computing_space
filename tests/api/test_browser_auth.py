import time
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loom_v2.observer.browser_auth import BrowserAuth


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
