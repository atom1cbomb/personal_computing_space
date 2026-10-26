"""AutoPaper queries with an isolated, browser-authenticated session per activation."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

DEFAULT_ORIGIN = "https://paper.cs.ac.cn"
COOKIE_NAME = "ap_sid"
MAX_BODY = 128 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
SCHEMAS = Path(__file__).with_name("schemas")
ROUTES = {
    "/v1/papers/search": ("papers-search", "POST", "/api/search"),
    "/v1/papers/get": ("papers-get", "GET", "/api/papers/{paper_id}"),
    "/v1/papers/list": ("papers-list", "GET", "/api/papers/list"),
    "/v1/venues/list": ("venues-list", "GET", "/api/papers/venues"),
    "/v1/years/list": ("years-list", "GET", "/api/papers/years"),
    "/v1/venues/catalog": ("venues-catalog", "GET", "/api/venues"),
}


class GatewayError(Exception):
    def __init__(self, code: str, status: int = 502):
        super().__init__(code)
        self.code, self.status = code, status


def reject_constant(_value: str):
    raise ValueError("nonfinite JSON number")


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def https_origin(value: Any) -> str:
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError("origin")
        parsed.port
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError) as exc:
        raise GatewayError("instance_config_invalid", 500) from exc


def valid_cookie(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}', value))


class AutoPaperGateway:
    def __init__(self, *, instance_path: Path = Path("/run/loom/instance.json"),
                 state_root: Path = Path("/var/lib/loom/service"),
                 transport: httpx.BaseTransport | None = None, timeout: float = 120):
        try:
            config = json.loads(instance_path.read_text()) if instance_path.exists() else {}
            config_schema = json.loads((SCHEMAS / "instance-config.json").read_text())
            if not Draft202012Validator(config_schema).is_valid(config):
                raise ValueError("config")
            self.origin = https_origin(config.get("api_origin", DEFAULT_ORIGIN))
        except (OSError, ValueError, TypeError) as exc:
            raise GatewayError("instance_config_invalid", 500) from exc
        self.validators = {
            action: {direction: Draft202012Validator(json.loads(
                (SCHEMAS / f"{action}-{direction}.json").read_text()))
                     for direction in ("input", "output")}
            for action, _, _ in ROUTES.values()
        }
        self.state_path = state_root / "autopaper-session.json"
        self._cookie: str | None = None
        self._browser_session: dict[str, Any] | None = None
        self._lock = threading.RLock()
        try:
            saved = json.loads(self.state_path.read_text())
            if isinstance(saved, dict) and saved.get("origin") == self.origin and valid_cookie(saved.get("cookie")):
                self._cookie = saved["cookie"]
        except (FileNotFoundError, ValueError):
            pass
        except OSError as exc:
            raise GatewayError("credential_state_unavailable", 503) from exc
        self.timeout = timeout
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def _persist_session(self, cookie: str):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.state_path.with_suffix(".tmp")
            with open(temporary, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                json.dump({"origin": self.origin, "cookie": cookie}, stream)
            temporary.replace(self.state_path)
        except OSError as exc:
            raise GatewayError("credential_state_unavailable", 503) from exc

    def _request_json(self, method: str, path: str, payload: dict[str, Any],
                      cookie: str, *, timeout: float) -> Any:
        # Construct the request directly: never reuse cookies from the client's jar.
        request = httpx.Request(
            method, self.origin + path,
            params=payload if method == "GET" else None,
            json=payload if method == "POST" else None,
            headers={"Accept": "application/json", "Cookie": f"{COOKIE_NAME}={cookie}"},
            extensions={"timeout": httpx.Timeout(timeout).as_dict()},
        )
        try:
            response = self.client.send(request, stream=True)
            try:
                if response.status_code == 401 or response.is_redirect:
                    raise GatewayError("autopaper_authentication_required", 401)
                if response.status_code == 403:
                    raise GatewayError("autopaper_forbidden", 403)
                if response.status_code == 404:
                    raise GatewayError("autopaper_not_found", 404)
                if response.status_code == 429:
                    raise GatewayError("autopaper_rate_limited", 429)
                if not 200 <= response.status_code < 300:
                    raise GatewayError("autopaper_request_failed")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise GatewayError("autopaper_response_too_large")
                return json.loads(raw, parse_constant=reject_constant, parse_float=finite_float)
            finally:
                response.close()
        except httpx.TimeoutException as exc:
            raise GatewayError("autopaper_timeout", 504) from exc
        except httpx.HTTPError as exc:
            raise GatewayError("autopaper_unreachable") from exc
        except (ValueError, UnicodeError) as exc:
            raise GatewayError("autopaper_response_invalid") from exc

    def _verify(self, cookie: str):
        value = self._request_json("GET", "/api/papers/venues", {}, cookie, timeout=10)
        self._result("venues-list", value)

    def auth_status(self) -> dict[str, Any]:
        with self._lock:
            if self._browser_session:
                if self._browser_session["expires_at"] > time.time():
                    return {"status": "auth_required", "poll_after_seconds": 2}
                self._browser_session = None
            if not self._cookie:
                return {"status": "auth_required", "poll_after_seconds": 5}
            try:
                self._verify(self._cookie)
                return {"status": "authenticated"}
            except GatewayError as exc:
                if exc.code == "autopaper_authentication_required":
                    return {"status": "auth_required", "poll_after_seconds": 5}
                return {"status": "auth_failed"}

    def auth_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload:
            return self.auth_status()
        session_id, expiry = payload.get("session_id"), payload.get("expires_at")
        if (set(payload) != {"session_id", "callback_uri", "expires_at"}
                or not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_id)
                or payload.get("callback_uri") != "http://localhost:3000/login-callback"
                or isinstance(expiry, bool) or not isinstance(expiry, (float, int))
                or not time.time() < expiry <= time.time() + 310):
            raise GatewayError("browser_context_invalid", 400)
        with self._lock:
            try:
                self.state_path.unlink(missing_ok=True)
            except OSError as exc:
                raise GatewayError("credential_state_unavailable", 503) from exc
            self._cookie = None
            self._browser_session = {**payload, "phase": "created"}
        return {"status": "auth_required", "poll_after_seconds": 2}

    def browser(self, payload: dict[str, Any]) -> dict[str, Any]:
        action, query = payload.get("action"), payload.get("query", {})
        if (set(payload) - {"action", "session_id", "query"} or action not in ("open", "callback")
                or not isinstance(query, dict) or action == "open" and query):
            raise GatewayError("browser_request_invalid", 400)
        with self._lock:
            session = self._browser_session
            if session is None or session["session_id"] != payload.get("session_id"):
                raise GatewayError("browser_session_not_found", 404)
            if session["expires_at"] <= time.time():
                self._browser_session = None
                raise GatewayError("browser_session_expired", 409)
            if session["phase"] != ("created" if action == "open" else "bound"):
                raise GatewayError("browser_session_consumed", 409)
            if action == "open":
                session["phase"] = "bound"
                return {"status": "redirect", "location": self.origin + "/oauth2/login"}
            values = query.get(COOKIE_NAME)
            if (set(query) != {COOKIE_NAME} or not isinstance(values, list) or len(values) != 1
                    or not valid_cookie(values[0])):
                raise GatewayError("browser_callback_invalid", 400)
            session["phase"] = "verifying"
            try:
                self._verify(values[0])
                self._persist_session(values[0])
                self._cookie = values[0]
            except GatewayError:
                raise GatewayError("browser_login_verification_failed", 409) from None
            finally:
                self._browser_session = None
            return {"status": "accepted"}

    def _result(self, action: str, value: Any) -> dict[str, Any]:
        result = {"action": action.replace("-", "_"), "result": value}
        if not self.validators[action]["output"].is_valid(result):
            raise GatewayError("autopaper_response_invalid")
        return result

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise GatewayError("parameters_invalid", 400)
        if path == "/v1/auth/start":
            return self.auth_start(payload)
        if path == "/v1/auth/status":
            if payload:
                raise GatewayError("parameters_invalid", 400)
            return self.auth_status()
        if path == "/v1/auth/browser":
            return self.browser(payload)
        route = ROUTES.get(path)
        if route is None:
            raise GatewayError("action_not_allowed", 404)
        action, method, upstream = route
        if not self.validators[action]["input"].is_valid(payload):
            raise GatewayError("parameters_invalid", 400)
        arguments = dict(payload)
        if action == "papers-search":
            if not math.isfinite(payload.get("min_similarity", 0.5)):
                raise GatewayError("parameters_invalid", 400)
            if not any(k.strip() for k in payload.get("keywords", [])) and not payload.get("description", "").strip():
                raise GatewayError("search_query_required", 400)
            start, end = payload.get("start_year"), payload.get("end_year")
            if start is not None and end is not None and start > end:
                raise GatewayError("year_range_invalid", 400)
            arguments = {"top_k": 50, "min_similarity": 0.5, **arguments, "auto_ingest": False}
        elif action == "papers-get":
            upstream = upstream.format(paper_id=arguments.pop("paper_id"))
        with self._lock:
            if not self._cookie or self._browser_session:
                raise GatewayError("autopaper_authentication_required", 401)
            cookie = self._cookie
        value = self._request_json(method, upstream, arguments, cookie, timeout=self.timeout)
        return self._result(action, value)


class Handler(BaseHTTPRequestHandler):
    gateway: AutoPaperGateway

    def _reply(self, status: int, value: Any):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._reply(200 if self.path == "/healthz" else 404,
                    {"status": "ok"} if self.path == "/healthz" else {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise GatewayError("request_too_large", 413)
            payload = json.loads(self.rfile.read(length) or b"{}", parse_constant=reject_constant,
                                 parse_float=finite_float)
            self._reply(200, self.gateway.handle(self.path, payload))
        except GatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, UnicodeError):
            self._reply(400, {"error": "parameters_invalid"})

    def log_message(self, _format, *_args):
        return


def main():
    gateway = AutoPaperGateway(
        instance_path=Path(os.getenv("AUTOPAPER_INSTANCE_PATH", "/run/loom/instance.json")),
        state_root=Path(os.getenv("AUTOPAPER_STATE_ROOT", "/var/lib/loom/service")),
    )
    Handler.gateway = gateway
    try:
        ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
