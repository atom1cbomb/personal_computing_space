"""Small SSH-over-HTTP gateway for one immutable VM activation.

The target is supplied by the runtime-owned ``/run/loom/instance.json`` file;
requests can only provide a remote command and timeout.  Credentials and
OpenSSH trust state live in the activation's persistent service volume.
"""

from __future__ import annotations

import json
from contextlib import suppress
import os
import re
import signal
import subprocess
import threading
import time
import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


from capability_services.ssh_execution import (
    DEFAULT_TIMEOUT, MAX_BODY, GatewayError, execute_ssh, validate_exec,
)

# Browser responses remain independent of the larger command request budget.
MAX_BROWSER_RESPONSE = 128 * 1024
ISSUER = "https://connect.cs.ac.cn"
LOCAL_CALLBACK = "http://localhost:3000/login-callback"


class VmSshGateway:
    def __init__(
        self,
        *,
        instance_path: Path = Path("/run/loom/instance.json"),
        state_root: Path = Path("/var/lib/loom/service"),
        opkssh: str = "opkssh",
        ssh: str = "ssh",
    ) -> None:
        self.instance_path = instance_path
        self.state_root = state_root
        self.opkssh = opkssh
        self.ssh = ssh
        self.ssh_dir = state_root / ".ssh"
        self.known_hosts = self.ssh_dir / "known_hosts"
        self.private_key = self.ssh_dir / "id_ecdsa"
        self._lock = threading.RLock()
        self._login_process: subprocess.Popen[bytes] | None = None
        self._browser_client: httpx.Client | None = None
        self._browser_session: dict[str, Any] | None = None
        self._expiry_timer: threading.Timer | None = None
        self._auth_failed = False

    def _target(self) -> dict[str, Any]:
        try:
            value = json.loads(self.instance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayError("instance_config_unavailable", 503) from exc
        if not isinstance(value, dict):
            raise GatewayError("instance_config_invalid", 503)
        host, user, port = value.get("host"), value.get("user"), value.get("port", 22)
        if (
            not isinstance(host, str)
            or not host
            or any(ord(char) < 33 or char in {"/", "\\"} for char in host)
            or not isinstance(user, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise GatewayError("instance_config_invalid", 503)
        return {"host": host, "user": user, "port": port}

    def _ssh_args(self) -> list[str]:
        target = self._target()
        self.ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        args = [
            self.ssh,
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "HostKeyAlias=loom-vm-" + self._resource_alias(),
            "-p",
            str(target["port"]),
        ]
        if self.private_key.is_file() and not self.private_key.is_symlink():
            args.extend(["-i", str(self.private_key)])
        args.append(f"{target['user']}@{target['host']}")
        return args

    def _resource_alias(self) -> str:
        try:
            value = json.loads(self.instance_path.read_text(encoding="utf-8"))
            raw_ref = value.get("target_resource_ref")
            ref = str(raw_ref.get("resource_id") if isinstance(raw_ref, dict) else raw_ref or "vm")
        except (OSError, json.JSONDecodeError):
            ref = "vm"
        return re.sub(r"[^A-Za-z0-9_.-]", "-", ref)[-96:] or "vm"

    def _credentials_present(self) -> bool:
        return self.private_key.is_file() and not self.private_key.is_symlink()

    def _ssh_probe(self) -> str:
        """Verify the newly-created PK key can actually reach the VM."""
        if not self._credentials_present():
            return "auth_required"
        args = [*self._ssh_args(), "true"]
        try:
            process = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return "auth_failed"
        if process.returncode == 0:
            return "authenticated"
        if b"Permission denied" in process.stderr:
            return "auth_required"
        return "auth_failed"

    def _clear_login(self, *, stop_process: bool) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        if self._browser_client is not None:
            self._browser_client.close()
            self._browser_client = None
        self._browser_session = None
        if stop_process and self._login_process is not None:
            process = self._login_process
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            self._login_process = None

    def close(self) -> None:
        with self._lock:
            self._clear_login(stop_process=True)

    def _expire(self, session_id: str) -> None:
        with self._lock:
            if self._browser_session and self._browser_session["session_id"] == session_id:
                self._clear_login(stop_process=True)
                self._auth_failed = False

    def auth_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Installation may probe this endpoint without creating a login.
        if not payload:
            return {"status": "auth_required", "poll_after_seconds": 2}
        session_id = payload.get("session_id")
        expires_at = payload.get("expires_at")
        if (set(payload) != {"session_id", "callback_uri", "expires_at"}
                or not isinstance(session_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_id)
                or payload.get("callback_uri") != LOCAL_CALLBACK
                or isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not time.time() < expires_at <= time.time() + 310):
            raise GatewayError("browser_context_invalid")
        with self._lock:
            self._clear_login(stop_process=True)
            self._auth_failed = False
            self._browser_session = {**payload, "phase": "created"}
            self._expiry_timer = threading.Timer(expires_at - time.time(), self._expire, args=(session_id,))
            self._expiry_timer.daemon = True
            self._expiry_timer.start()
        return {"status": "auth_required", "poll_after_seconds": 2}

    def _authorization_endpoint(self) -> str:
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
                response = client.get(ISSUER + "/.well-known/openid-configuration")
                response.raise_for_status()
                metadata = response.json()
            endpoint = metadata["authorization_endpoint"]
            parsed = urlparse(endpoint)
            if (metadata.get("issuer") != ISSUER or parsed.scheme != "https"
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("invalid endpoint")
            return endpoint
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise GatewayError("oidc_discovery_failed", 503) from exc

    def _start_login(self) -> None:
        self.ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._login_process = subprocess.Popen(
                [self.opkssh, "login", "--disable-browser-open",
                 "--config-path", str(Path(__file__).with_name("opkssh.yml")),
                 "--private-key-file", str(self.private_key)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.state_root)},
                start_new_session=True,
            )
        except OSError as exc:
            raise GatewayError("opkssh_unavailable", 503) from exc

    def _local_request(self, path: str, *, params=None) -> httpx.Response:
        # Bound even a compromised localhost response; cookies stay in this jar.
        with self._browser_client.stream("GET", path, params=params) as response:
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BROWSER_RESPONSE:
                    raise GatewayError("browser_response_too_large", 502)
            return response

    def browser(self, payload: dict[str, Any]) -> dict[str, Any]:
        action, session_id = payload.get("action"), payload.get("session_id")
        query = payload.get("query", {})
        if (action not in {"open", "callback"}
                or set(payload) - {"action", "session_id", "query"}
                or not isinstance(query, dict)
                or any(not isinstance(k, str) or not isinstance(v, list)
                       or not all(isinstance(item, str) for item in v) for k, v in query.items())
                or sum(len(v) for v in query.values()) > 32
                or len(json.dumps(query).encode()) > 65536
                or (action == "open" and query)):
            raise GatewayError("browser_request_invalid")
        with self._lock:
            session = self._browser_session
            if not session or session["session_id"] != session_id:
                raise GatewayError("browser_session_not_found", 404)
            if session["expires_at"] <= time.time():
                self._expire(session_id)
                raise GatewayError("browser_session_expired", 409)
            if session["phase"] != ("created" if action == "open" else "bound"):
                raise GatewayError("browser_session_consumed", 409)
            session["phase"] = "opening" if action == "open" else "callback"
            try:
                if action == "open":
                    endpoint = self._authorization_endpoint()
                    self._start_login()
                    self._browser_client = httpx.Client(
                        base_url="http://localhost:3000", trust_env=False,
                        follow_redirects=False, timeout=10,
                    )
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            response = self._local_request("/login")
                            break
                        except httpx.ConnectError:
                            if time.monotonic() >= deadline or self._login_process.poll() is not None:
                                raise GatewayError("browser_login_unavailable", 503)
                            time.sleep(0.1)
                    location = response.headers.get("location", "")
                    actual, expected = urlparse(location), urlparse(endpoint)
                    if (response.status_code not in {302, 303, 307, 308}
                            or len(location) > 16384 or actual.username or actual.password
                            or actual.fragment
                            or (actual.scheme, actual.hostname, actual.port or 443, actual.path)
                            != (expected.scheme, expected.hostname, expected.port or 443, expected.path)
                            or parse_qs(actual.query).get("redirect_uri") != [LOCAL_CALLBACK]):
                        raise GatewayError("browser_authorization_endpoint_invalid", 502)
                    session["phase"] = "bound"
                    return {"status": "redirect", "location": location}
                # The phase is consumed before forwarding. Never retry the code.
                response = self._local_request("/login-callback",
                    params=[(k, item) for k, values in query.items() for item in values])
                if response.status_code != 200:
                    raise GatewayError("browser_callback_rejected", 409)
                self._clear_login(stop_process=False)
                # Allow the official process to finish writing credentials.
                try:
                    self._login_process.wait(timeout=10)
                except subprocess.TimeoutExpired as exc:
                    raise GatewayError("browser_login_incomplete", 503) from exc
                if self._login_process.returncode != 0:
                    raise GatewayError("browser_login_failed", 409)
                return {"status": "accepted"}
            except (GatewayError, httpx.HTTPError, ValueError) as exc:
                self._clear_login(stop_process=True)
                self._auth_failed = True
                if isinstance(exc, GatewayError):
                    raise
                raise GatewayError("browser_proxy_failed", 502) from exc

    def auth_status(self) -> dict[str, Any]:
        with self._lock:
            if self._browser_session is not None:
                return {"status": "auth_required", "poll_after_seconds": 2}
            if self._auth_failed:
                return {"status": "auth_failed"}
            process = self._login_process
            if process is not None and process.poll() != 0:
                return {"status": "auth_required" if process.poll() is None else "auth_failed"}
            return {"status": self._ssh_probe()}

    def exec(self, command: Any, timeout_seconds: Any = DEFAULT_TIMEOUT) -> dict[str, Any]:
        timeout = validate_exec(command, timeout_seconds)
        return execute_ssh(self._ssh_args(), command, timeout)

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/auth/start":
            return self.auth_start(payload)
        if path == "/v1/auth/status":
            return self.auth_status()
        if path == "/v1/auth/browser":
            return self.browser(payload)
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
        state_root=Path(os.getenv("LOOM_SERVICE_STATE", "/var/lib/loom/service")),
    )
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
