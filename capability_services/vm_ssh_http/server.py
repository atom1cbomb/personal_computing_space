"""Activation-owned SSH target and credentials for commands and file streams.

The VM target is supplied by the runtime-owned instance configuration. Requests
provide operation arguments; host, user, port, SSH options and key paths remain
deployment-owned.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from capability_services.ssh_execution import (
    DEFAULT_TIMEOUT, GatewayError, execute_ssh, validate_exec,
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

    def execution_ssh_args(self) -> list[str]:
        return self._ssh_args()

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
        return execute_ssh(self.execution_ssh_args(), command, timeout)

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/auth/status":
            return self.auth_status()
        if path == "/v1/auth/start":
            return self.auth_start(payload)
        if path == "/v1/exec":
            return self.exec(payload.get("command"), payload.get("timeout_seconds", DEFAULT_TIMEOUT))
        raise GatewayError("not_found", 404)


def main() -> None:
    gateway = VmSshGateway(
        instance_path=Path(os.getenv("LOOM_INSTANCE_CONFIG", "/run/loom/instance.json")),
        credential_root=Path(os.getenv("LOOM_CREDENTIAL_ROOT", "/run/loom/credentials")),
        state_root=Path(os.getenv("LOOM_SERVICE_STATE", "/var/lib/loom/service")),
    )
    import uvicorn
    from capability_services.ssh_app import create_app
    uvicorn.run(create_app(gateway), host="0.0.0.0", port=int(os.getenv("PORT", "8080")),
                ws_max_size=65536, ws_max_queue=16)


if __name__ == "__main__":
    main()
