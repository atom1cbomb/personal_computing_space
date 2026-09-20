"""Policy-bound HTTP gateway for the Suanli Cloud control-plane API.

The gateway deliberately exposes named operations instead of forwarding an
arbitrary URL or Action.  AK/SK material is read only from the mounted
operator credential profile and is never returned in an error or response.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
DEFAULT_ENDPOINT = "https://api.cloud.cs.ac.cn"

# action, path, signing service.  The provider's Action discriminator belongs
# in the JSON body; it is not a query parameter on the wire.
OPERATIONS: dict[str, tuple[str, str, str]] = {
    "regions_list": ("DescribeRegions", "/v1/meta", "meta"),
    "zones_list": ("DescribeZones", "/v1/meta", "meta"),
    "nodes_list": ("DescribeNodes", "/v1/vm", "vm"),
    "instances_list": ("DescribeInstances", "/v1/vm", "vm"),
    "instances_status": ("DescribeInstanceStatus", "/v1/vm", "vm"),
    "images_list": ("DescribeImages", "/v1/vm", "vm"),
    "instances_run": ("RunInstances", "/v1/vm", "vm"),
    "instances_start": ("StartInstances", "/v1/vm", "vm"),
    "instances_stop": ("StopInstances", "/v1/vm", "vm"),
    "instances_reboot": ("RebootInstances", "/v1/vm", "vm"),
    "instances_shutdown": ("ShutdownInstances", "/v1/vm", "vm"),
    "instances_terminate": ("TerminateInstances", "/v1/vm", "vm"),
}


def _signing_region(action: str, parameters: dict[str, Any]) -> str:
    """Return the provider signing scope region for an action.

    Global metadata actions use the literal ``global``.  VM actions use the
    request's RegionId when supplied; the provider also accepts ``global`` for
    cross-region reads and requests whose instance IDs are resolved server-side.
    """
    if action in {"DescribeRegions", "DescribeZones", "DescribeImages"}:
        return "global"
    value = parameters.get("RegionId")
    return value if isinstance(value, str) and value else "global"


def _canonical_path(path: str) -> str:
    # urllib.parse.quote operates on UTF-8 and uses uppercase %XX escapes.
    from urllib.parse import quote

    return quote(path or "/", safe="/-_.~")


def _canonical_request(method: str, target: str, headers: dict[str, str], body: bytes) -> str:
    parsed = urlparse(target)
    host = headers["Host"].strip()
    date = headers["X-Suanli-Date"].strip()
    canonical_headers = f"host:{host}\nx-suanli-date:{date}\n"
    return "\n".join(
        (
            method.upper(),
            _canonical_path(parsed.path),
            parsed.query,
            canonical_headers,
            "host;x-suanli-date",
            hashlib.sha256(body).hexdigest(),
        )
    )


def _build_auth_headers(
    access_key_id: str,
    secret_key: str,
    endpoint: str,
    method: str,
    target: str,
    body: bytes,
    region: str,
    service: str,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, str]:
    """Build the documented SUANLI-HMAC-SHA256 headers.

    This is intentionally local to the capability container.  The secret is
    only used as HMAC key material and never included in a request payload.
    """
    parsed_endpoint = urlparse(endpoint)
    host = parsed_endpoint.netloc
    if not host or parsed_endpoint.username or parsed_endpoint.password:
        raise SuanliGatewayError("endpoint_invalid", 500)
    timestamp = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).replace(microsecond=0)
    date_value = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    short_date = timestamp.strftime("%Y%m%d")
    headers = {"Host": host, "X-Suanli-Date": date_value}
    canonical = _canonical_request(method, target, headers, body)
    scope = f"{short_date}/{region}/{service}/suanli_request"
    string_to_sign = "\n".join(
        ("SUANLI-HMAC-SHA256", date_value, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
    )

    def mac(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    k_date = mac(b"SUANLI" + secret_key.encode("utf-8"), short_date)
    k_region = mac(k_date, region)
    k_service = mac(k_region, service)
    k_signing = mac(k_service, "suanli_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"SUANLI-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders=host;x-suanli-date, Signature={signature}"
    )
    return headers


class SuanliGatewayError(Exception):
    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class SuanliGateway:
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
        if parsed.scheme != "https" or not parsed.netloc:
            raise SuanliGatewayError("endpoint_invalid", 500)
        self.timeout = timeout

    def _secret(self, filename: str) -> str:
        path = self.credential_root / filename
        try:
            if path.is_symlink() or not path.is_file():
                raise SuanliGatewayError("credential_profile_unavailable", 503)
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SuanliGatewayError("credential_profile_unavailable", 503) from exc
        if not value or len(value) > 512 or any(ord(ch) < 32 for ch in value):
            raise SuanliGatewayError("credential_profile_invalid", 503)
        return value

    def _auth_headers(
        self,
        method: str,
        target: str,
        body: bytes,
        operation: str,
        parameters: dict[str, Any],
    ) -> dict[str, str]:
        action, _path, service = OPERATIONS[operation]
        return _build_auth_headers(
            self._secret("AccessKeyId"),
            self._secret("SecretKey"),
            self.endpoint,
            method,
            target,
            body,
            _signing_region(action, parameters),
            service,
        )

    def call(self, operation: str, parameters: dict[str, Any] | None = None) -> Any:
        if operation not in OPERATIONS:
            raise SuanliGatewayError("operation_not_allowed", 400)
        action, path, _service = OPERATIONS[operation]
        body_value = dict(parameters or {})
        body_value["Action"] = action
        body = json.dumps(body_value, separators=(",", ":"), ensure_ascii=False).encode()
        if len(body) > MAX_BODY:
            raise SuanliGatewayError("request_too_large", 413)
        target = path
        headers = self._auth_headers("POST", target, body, operation, body_value)
        headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with httpx.stream(
                "POST", self.endpoint + target, content=body, headers=headers,
                timeout=self.timeout, follow_redirects=False,
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise SuanliGatewayError("provider_response_too_large", 502)
                status = response.status_code
        except httpx.HTTPError as exc:
            raise SuanliGatewayError("provider_unreachable", 502) from exc
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SuanliGatewayError("provider_invalid_json", 502) from exc
        if not 200 <= status < 300:
            error = result.get("Error") if isinstance(result, dict) else None
            code = error.get("Code") if isinstance(error, dict) else None
            if code in {"InvalidAccessKey", "SignatureDoesNotMatch", "MissingAuthenticationToken"}:
                raise SuanliGatewayError("provider_auth_failed", 502)
            raise SuanliGatewayError("provider_request_failed", 502)
        return result

    def auth_status(self) -> dict[str, Any]:
        try:
            self.call("regions_list")
        except SuanliGatewayError as exc:
            if exc.code == "provider_auth_failed":
                return {"status": "auth_failed"}
            return {"status": "auth_failed", "retryable": exc.code == "provider_unreachable"}
        return {"status": "authenticated"}

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path in {"/v1/auth/start", "/v1/auth/status"}:
            return self.auth_status()
        if path not in {"/v1/" + name.replace("_", "/") for name in OPERATIONS}:
            raise SuanliGatewayError("operation_not_allowed", 404)
        operation = path.removeprefix("/v1/").replace("/", "_")
        parameters = payload.get("parameters", payload)
        if not isinstance(parameters, dict):
            raise SuanliGatewayError("request_json_invalid", 400)
        return {"operation": operation, "result": self.call(operation, parameters)}


class Handler(BaseHTTPRequestHandler):
    gateway: SuanliGateway

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
                raise SuanliGatewayError("request_too_large", 413)
            raw = self.rfile.read(length)
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise SuanliGatewayError("request_json_invalid", 400)
            result = self.gateway.handle(self.path.split("?", 1)[0], payload)
            self._reply(200, result)
        except SuanliGatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "request_json_invalid"})
        except Exception:
            self._reply(500, {"error": "gateway_error"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    gateway = SuanliGateway(
        credential_root=Path(os.getenv("SUANLI_CREDENTIAL_ROOT", "/run/loom/credentials")),
        endpoint=os.getenv("SUANLI_ENDPOINT", DEFAULT_ENDPOINT),
        timeout=float(os.getenv("SUANLI_TIMEOUT_SECONDS", "20")),
    )
    Handler.gateway = gateway
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
