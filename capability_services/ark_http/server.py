"""Ark data-plane actions with an activation-scoped, user-supplied API key."""
from __future__ import annotations

import base64
import binascii
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator


SCHEMAS = Path(__file__).with_name("schemas")
MAX_BODY = 32 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
BASE_URLS = {
    "agent-plan": "https://ark.cn-beijing.volces.com/api/plan/v3",
    "standard": "https://ark.cn-beijing.volces.com/api/v3",
}
SPEECH_URL = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
VIDEO_PATH = "/contents/generations/tasks"
ROUTES = {
    "/v1/chat/completions": "chat-completions",
    "/v1/responses/create": "responses-create",
    "/v1/responses/get": "responses-get",
    "/v1/responses/delete": "responses-delete",
    "/v1/images/generate": "images-generate",
    "/v1/embeddings/multimodal": "embeddings-multimodal",
    "/v1/videos/create": "videos-create",
    "/v1/videos/get": "videos-get",
    "/v1/videos/list": "videos-list",
    "/v1/videos/delete": "videos-delete",
    "/v1/audio/speech": "audio-speech",
}
POST_PATHS = {
    "chat-completions": "/chat/completions",
    "responses-create": "/responses",
    "images-generate": "/images/generations",
    "embeddings-multimodal": "/embeddings/multimodal",
    "videos-create": VIDEO_PATH,
}


class ArkGatewayError(Exception):
    """Stable errors containing no credential, prompt or upstream response body."""

    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _invalid_constant(_value: str) -> None:
    raise ValueError("nonfinite_json")


def load_json(raw: bytes) -> Any:
    return json.loads(raw, parse_constant=_invalid_constant)


class ArkGateway:
    def __init__(
        self,
        *,
        instance_path: Path = Path("/run/loom/instance.json"),
        credential_root: Path = Path("/run/loom/credentials"),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            config = load_json(instance_path.read_bytes()) if instance_path.exists() else {}
        except (OSError, ValueError) as exc:
            raise ArkGatewayError("instance_config_invalid", 500) from exc
        if isinstance(config, dict):
            # The runtime adds this transport metadata to every mounted config;
            # it is separate from the package's caller-supplied configuration.
            config.pop("target_resource_ref", None)
        schema = json.loads((SCHEMAS / "instance-config.json").read_text())
        if not Draft202012Validator(schema).is_valid(config):
            raise ArkGatewayError("instance_config_invalid", 500)
        self.api_mode = config.get("api_mode", "agent-plan")
        self.base_url = BASE_URLS[self.api_mode]
        self.timeout = float(config.get("timeout_seconds", 120))
        self.credential_root = credential_root
        self.transport = transport
        self.validators = {
            (action, direction): Draft202012Validator(
                json.loads((SCHEMAS / f"{action}-{direction}.json").read_text())
            )
            for action in ROUTES.values()
            for direction in ("input", "output")
        }

    def _api_key(self) -> str:
        path = self.credential_root / "api_key"
        try:
            if path.is_symlink() or not path.is_file():
                raise ArkGatewayError("credential_profile_unavailable", 503)
            with path.open("rb") as source:
                raw = source.read(4097)
            value = raw.decode("ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise ArkGatewayError("credential_profile_invalid", 503) from exc
        if len(raw) > 4096 or not value or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in value):
            raise ArkGatewayError("credential_profile_invalid", 503)
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: Any = None,
        speech: bool = False,
        timeout: float | None = None,
    ) -> bytes:
        key = self._api_key()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if speech:
            headers.update({"X-Api-Key": key, "X-Api-Resource-Id": "seed-tts-2.0",
                            "X-Control-Require-Usage-Tokens-Return": "*"})
        else:
            headers["Authorization"] = f"Bearer {key}"
        try:
            content = None if body is None else json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ArkGatewayError("invalid_input", 400) from exc
        if content is not None and len(content) > MAX_BODY:
            raise ArkGatewayError("request_too_large", 413)
        try:
            # A new client per exchange keeps concurrent HTTP handlers isolated.
            # httpx's transport performs no retry; redirects never receive the key.
            with httpx.Client(transport=self.transport, follow_redirects=False,
                              timeout=timeout or self.timeout) as client:
                with client.stream(method, SPEECH_URL if speech else self.base_url + path,
                                   content=content, params=params, headers=headers) as response:
                    status = response.status_code
                    if not 200 <= status < 300:
                        code, error_status = {
                            401: ("provider_auth_failed", 401),
                            403: ("provider_forbidden", 403),
                            404: ("provider_not_found", 404),
                            429: ("provider_rate_limited", 429),
                        }.get(status, ("provider_unavailable", 502) if status >= 500
                              else ("provider_request_failed", 502))
                        raise ArkGatewayError(code, error_status)
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            raise ArkGatewayError("provider_response_too_large")
                        raw.extend(chunk)
                    return bytes(raw)
        except httpx.TimeoutException as exc:
            raise ArkGatewayError("provider_timeout", 504) from exc
        except httpx.HTTPError as exc:
            raise ArkGatewayError("provider_unreachable") from exc

    def _json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        raw = self._request(method, path, **kwargs)
        # Some DELETE implementations return HTTP 204 rather than {}.
        if method == "DELETE" and not raw:
            return {}
        try:
            result = load_json(raw)
        except (ValueError, UnicodeError) as exc:
            raise ArkGatewayError("provider_invalid_json") from exc
        if not isinstance(result, dict):
            raise ArkGatewayError("provider_response_invalid")
        # A failed video task or Response is a valid query result; keep its
        # status and error so callers can distinguish acceptance from success.
        if result.get("error") and "status" not in result:
            raise ArkGatewayError("provider_request_failed")
        return result

    def auth_status(self) -> dict[str, str]:
        """Verify the key with a documented, non-generating data-plane read."""
        try:
            result = self._json_request("GET", VIDEO_PATH,
                                        params={"page_num": 1, "page_size": 1},
                                        timeout=min(self.timeout, 10))
            if not isinstance(result.get("items"), list):
                raise ArkGatewayError("provider_response_invalid")
        except ArkGatewayError as exc:
            if exc.code in {"credential_profile_unavailable", "provider_auth_failed"}:
                return {"status": "auth_required"}
            return {"status": "auth_failed"}
        return {"status": "authenticated"}

    def _speech(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.api_mode != "agent-plan":
            raise ArkGatewayError("action_requires_agent_plan", 400)
        raw = self._request("POST", "", body=arguments, speech=True)
        audio = bytearray()
        usage: dict[str, Any] = {}
        finished = False
        try:
            for line in raw.splitlines():
                if not line.strip():
                    continue
                event = load_json(line)
                if not isinstance(event, dict) or not isinstance(event.get("code"), int):
                    raise ArkGatewayError("provider_response_invalid")
                code = event["code"]
                if code not in (0, 20000000):
                    raise ArkGatewayError("provider_speech_failed")
                if event.get("data"):
                    audio.extend(base64.b64decode(event["data"], validate=True))
                if isinstance(event.get("usage"), dict):
                    usage.update(event["usage"])
                if code == 20000000:
                    finished = True
                    break
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ArkGatewayError("provider_response_invalid") from exc
        if not finished or not audio:
            raise ArkGatewayError("provider_speech_incomplete")
        return {"audio_base64": base64.b64encode(audio).decode("ascii"),
                "audio_format": arguments["req_params"]["audio_params"]["format"],
                "usage": usage}

    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action not in ROUTES.values():
            raise ArkGatewayError("action_not_allowed", 404)
        if not self.validators[action, "input"].is_valid(arguments):
            raise ArkGatewayError("invalid_input", 400)
        if action == "images-generate" and not (
            arguments.get("prompt", "").strip()
            or (arguments.get("layer_decomposition") and arguments.get("image"))
        ):
            raise ArkGatewayError("invalid_input", 400)
        if action in POST_PATHS:
            result = self._json_request("POST", POST_PATHS[action], body=arguments)
        elif action.startswith("responses-"):
            method = "GET" if action == "responses-get" else "DELETE"
            result = self._json_request(method, "/responses/" + arguments["response_id"],
                                        params={"include": arguments["include"]} if "include" in arguments else None)
        elif action in {"videos-get", "videos-delete"}:
            method = "GET" if action == "videos-get" else "DELETE"
            result = self._json_request(method, VIDEO_PATH + "/" + arguments["task_id"])
        elif action == "videos-list":
            query = {key: value for key, value in arguments.items() if key != "filter"}
            query.update({"filter." + key: value for key, value in arguments.get("filter", {}).items()})
            result = self._json_request("GET", VIDEO_PATH, params=query)
        else:
            result = self._speech(arguments)
        wrapped = {"action": action.replace("-", "_"), "result": result}
        if not self.validators[action, "output"].is_valid(wrapped):
            raise ArkGatewayError("provider_response_invalid")
        if action == "images-generate" and not any(
            item.get("url") or item.get("b64_json") for item in result["data"]
        ):
            raise ArkGatewayError("provider_generation_failed")
        return wrapped

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path in {"/v1/auth/start", "/v1/auth/status"}:
            return self.auth_status()
        action = ROUTES.get(path)
        if action is None:
            raise ArkGatewayError("action_not_allowed", 404)
        return self.call(action, payload)


class Handler(BaseHTTPRequestHandler):
    gateway: ArkGateway

    def _reply(self, status: int, value: Any) -> None:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise ArkGatewayError("request_too_large", 413)
            payload = load_json(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ArkGatewayError("invalid_input", 400)
            self._reply(200, self.gateway.handle(self.path, payload))
        except ArkGatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, UnicodeError):
            self._reply(400, {"error": "invalid_input"})
        except Exception:
            self._reply(500, {"error": "gateway_error"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    Handler.gateway = ArkGateway(
        instance_path=Path(os.getenv("LOOM_INSTANCE_PATH", "/run/loom/instance.json")),
        credential_root=Path(os.getenv("ARK_CREDENTIAL_ROOT", "/run/loom/credentials")),
    )
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
