from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from loom_v2.observer.browser_auth import BrowserAuth


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "capability_services/suanli_vm_ssh_http/client/loom_vm_auth.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def test_local_helper_forwards_only_fixed_browser_routes() -> None:
    calls: list[tuple[dict, str]] = []

    class BridgeHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            calls.append((payload, self.headers.get("Authorization", "")))
            if payload["action"] == "open":
                body = {"status": "redirect", "location": "https://idp.example/authorize?state=x",
                        "browser_binding": "binding-1"}
            else:
                body = {"status": "accepted"}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    bridge_server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
    bridge_port = bridge_server.server_address[1]
    import threading
    bridge_thread = threading.Thread(target=bridge_server.serve_forever, daemon=True)
    bridge_thread.start()
    local_port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--bridge-url", f"http://127.0.0.1:{bridge_port}/bridge",
         "--session-id", "session-1", "--session-token", "token-1", "--port", str(local_port)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(50):
            try:
                opener = build_opener(_NoRedirect())
                request = Request(f"http://127.0.0.1:{local_port}/login?session_id=session-1")
                with opener.open(request, timeout=1):
                    raise AssertionError("unexpected redirect follow")
            except HTTPError as exc:
                assert exc.code == 302
                assert exc.headers["Location"].startswith("https://idp.example/")
                cookie = exc.headers["Set-Cookie"].split(";", 1)[0]
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise AssertionError("local helper did not start")

        callback = Request(
            f"http://127.0.0.1:{local_port}/login-callback?code=abc&state=x",
            headers={"Cookie": cookie},
        )
        try:
            build_opener(_NoRedirect()).open(callback, timeout=2)
        except HTTPError as exc:
            assert exc.code == 303
            assert exc.headers["Location"] == "/result"
        assert len(calls) == 2
        assert calls[0][0]["action"] == "open"
        assert calls[1][0]["action"] == "callback"
        assert calls[1][0]["query"] == {"code": ["abc"], "state": ["x"]}
        assert all(auth == "Bearer token-1" for _, auth in calls)
        assert "session_token" not in calls[0][0]
    finally:
        process.terminate()
        process.wait(timeout=5)
        bridge_server.shutdown()


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
