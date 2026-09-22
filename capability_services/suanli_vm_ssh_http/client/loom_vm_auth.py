#!/usr/bin/env python3
"""Local browser bridge for the Suanli VM SSH capability.

This process only exposes the two fixed localhost callback routes.  It does
not open a tunnel, proxy arbitrary URLs, or require an Observer host account.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


COOKIE = "loom_browser_auth"
MAX_QUERY = 65536


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Bridge:
    def __init__(self, url: str, session_id: str, token: str):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("bridge_url_invalid")
        self.url = url
        self.session_id = session_id
        self.token = token
        self.binding: str | None = None
        self.opener = build_opener(NoRedirect())

    def call(self, action: str, query: dict[str, list[str]] | None = None) -> dict:
        payload = {"action": action, "session_id": self.session_id, "query": query or {}}
        if self.binding is not None:
            payload["browser_binding"] = self.binding
        request = Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(128 * 1024)
        except HTTPError as exc:
            raise RuntimeError(f"observer_bridge_http_{exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("observer_bridge_unavailable") from exc
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("observer_bridge_invalid_response") from exc
        if not isinstance(value, dict):
            raise RuntimeError("observer_bridge_invalid_response")
        if action == "open":
            location = value.get("location")
            binding = value.get("browser_binding")
            if value.get("status") != "redirect" or not isinstance(location, str) or not isinstance(binding, str):
                raise RuntimeError("browser_auth_open_failed")
            self.binding = binding
        elif value.get("status") != "accepted":
            raise RuntimeError("browser_auth_callback_failed")
        return value


class Handler(BaseHTTPRequestHandler):
    bridge: Bridge

    def log_message(self, _format: str, *_args) -> None:
        # Authorization codes and query strings must not enter access logs.
        return

    def _reply(self, status: int, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/login":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) != {"session_id"} or query["session_id"] != [self.bridge.session_id]:
                    raise RuntimeError("browser_session_invalid")
                value = self.bridge.call("open")
                self._reply(HTTPStatus.FOUND, b"", headers={
                    "Location": value["location"],
                    "Set-Cookie": f"{COOKIE}={self.bridge.binding}; Max-Age=300; HttpOnly; SameSite=Lax; Path=/",
                })
                return
            if parsed.path == "/login-callback":
                if len(parsed.query.encode("utf-8")) > MAX_QUERY:
                    raise RuntimeError("browser_callback_invalid")
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                binding = cookie.get(COOKIE)
                if binding is None or self.bridge.binding != binding.value:
                    raise RuntimeError("browser_session_not_found")
                query = parse_qs(parsed.query, keep_blank_values=True)
                self.bridge.call("callback", query)
                self._reply(HTTPStatus.SEE_OTHER, b"Authentication callback received. Check instance status in Loom.\n",
                            headers={"Location": "/result", "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
                return
            if parsed.path == "/result":
                self._reply(HTTPStatus.OK, b"Authentication callback received. Check instance status in Loom.\n",
                            headers={"Content-Type": "text/plain; charset=utf-8"})
                return
            raise RuntimeError("not_found")
        except RuntimeError as exc:
            self._reply(HTTPStatus.BAD_GATEWAY, json.dumps({"error": str(exc)}).encode(),
                        headers={"Content-Type": "application/json"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-token", default=None)
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args(argv)
    token = args.session_token
    if not token:
        parser.error("--session-token is required")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    bridge = Bridge(args.bridge_url, args.session_id, token)
    Handler.bridge = bridge
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Open http://localhost:{args.port}/login?session_id={args.session_id}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
