"""Operator-owned credential broker for a single isolated evaluation container.

Run as UID 0 with a root-only config; the subject runs as UID 1000. This hides
upstream credentials, not authorization to the workspace. No request bodies,
headers, provider errors or credentials are logged. Destroy with the container.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def prepare(config, path, body, method):
    """Route only fixed upstreams; never honor caller-supplied forwarding URLs."""
    if not path.startswith("/") or path.startswith("//") or ".." in path.split("?")[0].split("/"):
        raise ValueError("path_not_allowed")
    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
    if path.startswith("/model/"):
        suffix = path.removeprefix("/model")
        if suffix not in {"/responses", "/chat/completions", "/models"}:
            raise ValueError("path_not_allowed")
        headers["Authorization"] = "Bearer " + config["model_api_key"]
        return config["model_base_url"].rstrip("/") + suffix, body, headers, True
    permitted = (path == "/.well-known/grip" or path in {"/healthz", "/api/v1/runtime"}
                 or path.startswith(("/api/v1/content", "/api/v1/capability-packages", "/api/v1/action-descriptors",
                                     "/api/v1/auth/token")))
    if not permitted:
        raise ValueError("path_not_allowed")
    if path == "/.well-known/grip":
        value = json.loads(body)
        if value.get("auth", {}).get("credential") == config["session_token"]:
            value["auth"]["credential"] = config["grip_token"]
        body = json.dumps(value).encode()
    headers["Authorization"] = "Bearer " + config["grip_token"]
    return config["observer"].rstrip("/") + path, body, headers, False


def rewrite_description(value, observer, proxy):
    # Only rewrite a GRIP description's binding locations. Business content,
    # source, schemas and their immutable digests must remain byte-equivalent.
    if value.get("kind") == "reply" and isinstance(value.get("body"), dict):
        body = value["body"]
        if "resource" in body and "offers" in body:
            for offer in body["offers"]:
                for binding in offer.get("bindings", []):
                    if binding.get("endpoint") == observer.rstrip("/") + "/.well-known/grip":
                        binding["endpoint"] = proxy + "/.well-known/grip"
    return value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    config: dict
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def log_message(self, *_args):
        pass

    def _error(self, status):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"validation_proxy_rejected"}')

    def handle_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 16 * 1024 * 1024:
                return self._error(413)
            body = self.rfile.read(length)
            url, body, headers, model = prepare(self.config, self.path, body, self.command)
            request = Request(url, data=body if self.command != "GET" else None,
                              headers=headers, method=self.command)
            try:
                response = self.opener.open(request, timeout=180)
            except HTTPError as exc:
                # Do not forward upstream errors that might echo auth headers.
                return self._error(exc.code)
            with response:
                content_type = response.headers.get("Content-Type", "application/json")
                if model and "text/event-stream" in content_type:
                    self.send_response(response.status)
                    self.send_header("Content-Type", content_type)
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    return self._error(502)
                if self.path == "/.well-known/grip":
                    raw = json.dumps(rewrite_description(json.loads(raw), self.config["observer"],
                                                        "http://127.0.0.1:8889")).encode()
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        except (ValueError, KeyError, TypeError):
            self._error(400)
        except (OSError, URLError):
            self._error(502)

    do_POST = handle_request
    do_GET = handle_request


if __name__ == "__main__":
    Handler.config = json.loads(Path(sys.argv[1]).read_text())
    for key in ("observer", "model_base_url"):
        parsed = urlsplit(Handler.config[key])
        assert parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
    ThreadingHTTPServer(("127.0.0.1", 8889), Handler).serve_forever()
