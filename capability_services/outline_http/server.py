"""Small, policy-bound HTTP gateway for an Outline instance.

The gateway exposes a fixed subset of Outline's JSON RPC methods.  It never
accepts an arbitrary method, URL, or HTTP verb from the caller.  The API key is
read from the operator-mounted credential profile and is used only to build a
Bearer header; it is never returned in a response or error.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
DEFAULT_ENDPOINT = "https://docs.cs.ac.cn/api"
API_KEY_FILENAME = "outline_key"

# These are deliberately named actions rather than a general Outline
# proxy.  The read actions support discovery and retrieval; the write
# actions cover normal document maintenance while leaving user/admin,
# attachment, API-key, and permanent-delete methods outside this package.
ACTIONS: dict[str, str] = {
    # Internal probe used by the generic authentication handshake. It is not
    # exposed as a capability export or as a /v1/<action> route.
    "auth_info": "/auth.info",
    "collections_list": "/collections.list",
    "collections_info": "/collections.info",
    "collections_documents": "/collections.documents",
    "documents_list": "/documents.list",
    "documents_info": "/documents.info",
    "documents_documents": "/documents.documents",
    "documents_search_titles": "/documents.search_titles",
    "documents_search": "/documents.search",
    "documents_create": "/documents.create",
    "documents_update": "/documents.update",
    "documents_archive": "/documents.archive",
    "documents_restore": "/documents.restore",
}

ROUTES: dict[str, str] = {
    "collections/list": "collections_list",
    "collections/info": "collections_info",
    "collections/documents": "collections_documents",
    "documents/list": "documents_list",
    "documents/info": "documents_info",
    "documents/documents": "documents_documents",
    "documents/search_titles": "documents_search_titles",
    "documents/search": "documents_search",
    "documents/create": "documents_create",
    "documents/update": "documents_update",
    "documents/archive": "documents_archive",
    "documents/restore": "documents_restore",
}

class OutlineGatewayError(Exception):
    """An error safe to expose to the capability runtime."""

    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class OutlineGateway:
    def __init__(
        self,
        *,
        credential_root: Path = Path("/run/loom/credentials"),
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 20.0,
    ) -> None:
        self.credential_root = credential_root
        self.endpoint = endpoint.rstrip("/")
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise OutlineGatewayError("endpoint_invalid", 500)
        if timeout <= 0:
            raise OutlineGatewayError("timeout_invalid", 500)
        self.timeout = float(timeout)

    def _api_key(self) -> str:
        path = self.credential_root / API_KEY_FILENAME
        try:
            if path.is_symlink() or not path.is_file():
                raise OutlineGatewayError("credential_profile_unavailable", 503)
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OutlineGatewayError("credential_profile_unavailable", 503) from exc
        if not value or len(value) > 512 or any(ord(ch) < 32 for ch in value):
            raise OutlineGatewayError("credential_profile_invalid", 503)
        return value

    @staticmethod
    def _json_body(parameters: dict[str, Any]) -> bytes:
        try:
            body = json.dumps(
                parameters,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise OutlineGatewayError("request_json_invalid", 400) from exc
        if len(body) > MAX_BODY:
            raise OutlineGatewayError("request_too_large", 413)
        return body

    def call(self, action: str, parameters: dict[str, Any] | None = None) -> Any:
        """Call one allowlisted Outline method and return its JSON response."""
        if action not in ACTIONS:
            raise OutlineGatewayError("action_not_allowed", 400)
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise OutlineGatewayError("request_json_invalid", 400)
        body = self._json_body(parameters)
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            with httpx.stream(
                "POST",
                self.endpoint + ACTIONS[action],
                content=body,
                headers=headers,
                timeout=self.timeout,
                follow_redirects=False,
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OutlineGatewayError("provider_response_too_large", 502)
                status = response.status_code
                retry_after = response.headers.get("Retry-After")
        except OutlineGatewayError:
            raise
        except httpx.HTTPError as exc:
            raise OutlineGatewayError("provider_unreachable", 502) from exc

        try:
            result = json.loads(bytes(raw))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OutlineGatewayError("provider_invalid_json", 502) from exc
        if not 200 <= status < 300:
            if status == 401:
                raise OutlineGatewayError("provider_auth_failed", 502)
            if status == 403:
                raise OutlineGatewayError("provider_forbidden", 502)
            if status == 429:
                # The retry hint is intentionally not propagated through the
                # package protocol; the caller can retry the next attempt.
                _ = retry_after
                raise OutlineGatewayError("provider_rate_limited", 429)
            if status >= 500:
                raise OutlineGatewayError("provider_unavailable", 502)
            raise OutlineGatewayError("provider_request_failed", 502)
        return result

    def auth_status(self) -> dict[str, Any]:
        """Probe the API key without returning Outline's user data."""
        try:
            self.call("auth_info")
        except OutlineGatewayError as exc:
            if exc.code == "provider_auth_failed":
                return {"status": "auth_failed"}
            return {
                "status": "auth_failed",
                "retryable": exc.code in {"provider_unreachable", "provider_unavailable"},
            }
        return {"status": "authenticated"}

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path in {"/v1/auth/start", "/v1/auth/status"}:
            return self.auth_status()
        if not path.startswith("/v1/"):
            raise OutlineGatewayError("action_not_allowed", 404)
        action = ROUTES.get(path.removeprefix("/v1/"))
        if action is None:
            raise OutlineGatewayError("action_not_allowed", 404)
        parameters = payload.get("parameters", payload)
        if not isinstance(parameters, dict):
            raise OutlineGatewayError("request_json_invalid", 400)
        return {"action": action, "result": self.call(action, parameters)}


class Handler(BaseHTTPRequestHandler):
    gateway: OutlineGateway

    def _reply(self, status: int, value: Any) -> None:
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, b"ok")
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise OutlineGatewayError("request_too_large", 413)
            raw = self.rfile.read(length)
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise OutlineGatewayError("request_json_invalid", 400)
            result = self.gateway.handle(self.path.split("?", 1)[0], payload)
            self._reply(200, result)
        except OutlineGatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "request_json_invalid"})
        except Exception:
            self._reply(500, {"error": "gateway_error"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    gateway = OutlineGateway(
        credential_root=Path(
            os.getenv("OUTLINE_CREDENTIAL_ROOT", "/run/loom/credentials")
        ),
        endpoint=os.getenv("OUTLINE_ENDPOINT", DEFAULT_ENDPOINT),
        timeout=float(os.getenv("OUTLINE_TIMEOUT_SECONDS", "20")),
    )
    Handler.gateway = gateway
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
