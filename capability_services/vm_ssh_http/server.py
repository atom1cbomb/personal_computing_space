"""Policy-bound HTTP gateway for executing commands on an ordinary SSH VM.

The VM target is supplied by the runtime-owned instance configuration. Requests
can provide only a command and timeout; host, user, port, SSH options and key
paths are deployment-owned.
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from capability_services.ssh_execution import (
    DEFAULT_TIMEOUT, MAX_BODY, GatewayError, execute_ssh, validate_exec,
)


class VmSshGateway:
    def __init__(
        self,
        *,
        instance_path: Path = Path("/run/loom/instance.json"),
        credential_root: Path = Path("/run/loom/credentials"),
        state_root: Path = Path("/var/lib/loom/service"),
        ssh: str = "ssh",
    ) -> None:
        self.instance_path = instance_path
        self.credential_root = credential_root
        self.state_root = state_root
        self.ssh = ssh
        self.known_hosts = state_root / ".ssh" / "known_hosts"
        self.private_key = credential_root / "id_ed25519"

    def _target(self) -> dict[str, Any]:
        try:
            value = json.loads(self.instance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayError("instance_config_unavailable", 503) from exc
        if not isinstance(value, dict):
            raise GatewayError("instance_config_invalid", 503)
        host, user, port = value.get("host"), value.get("user"), value.get("port", 22)
        if (
            not isinstance(host, str) or not host or len(host) > 255
            or any(ord(char) < 33 or char in {"/", "\\"} for char in host)
            or not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user)
            or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        ):
            raise GatewayError("instance_config_invalid", 503)
        return {"host": host, "user": user, "port": port}

    def _ssh_args(self) -> list[str]:
        target = self._target()
        if self.private_key.is_symlink() or not self.private_key.is_file():
            raise GatewayError("ssh_key_unavailable", 503)
        self.known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return [
            self.ssh, "-F", "/dev/null", "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "LogLevel=ERROR", "-i", str(self.private_key),
            "-p", str(target["port"]), "--", f"{target['user']}@{target['host']}",
        ]

    def auth_status(self) -> dict[str, Any]:
        if self.private_key.is_symlink() or not self.private_key.is_file():
            return {"status": "auth_required"}
        return {"status": "authenticated"}

    def auth_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            raise GatewayError("authentication_not_supported")
        return self.auth_status()

    def exec(self, command: Any, timeout_seconds: Any = DEFAULT_TIMEOUT) -> dict[str, Any]:
        timeout = validate_exec(command, timeout_seconds)
        return execute_ssh(self._ssh_args(), command, timeout)

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/auth/status":
            return self.auth_status()
        if path == "/v1/auth/start":
            return self.auth_start(payload)
        if path == "/v1/exec":
            return self.exec(payload.get("command"), payload.get("timeout_seconds", DEFAULT_TIMEOUT))
        raise GatewayError("not_found", 404)


class Handler(BaseHTTPRequestHandler):
    gateway: VmSshGateway

    def _reply(self, status: int, value: Any) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._reply(200, {"status": "ok"} if self.path == "/healthz" else {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise GatewayError("request_too_large", 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise GatewayError("request_json_invalid")
            self._reply(200, self.gateway.handle(self.path.split("?", 1)[0], payload))
        except GatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "request_json_invalid"})
        except Exception:
            self._reply(500, {"error": "gateway_error"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    Handler.gateway = VmSshGateway(
        instance_path=Path(os.getenv("LOOM_INSTANCE_CONFIG", "/run/loom/instance.json")),
        credential_root=Path(os.getenv("LOOM_CREDENTIAL_ROOT", "/run/loom/credentials")),
        state_root=Path(os.getenv("LOOM_SERVICE_STATE", "/var/lib/loom/service")),
    )
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
