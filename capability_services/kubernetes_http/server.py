"""Small, policy-bound HTTP gateway for a user's Kubernetes namespace.

The image intentionally uses the kubectl CLI because the supported cluster
authentication is the kubeconfig exec plugin from production/kubernetes.md.
Only fixed actions are exposed; request data is never interpolated into a
shell command.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import signal
import threading
import contextlib
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MAX_BODY = 2 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
COMMAND_TIMEOUT = float(os.getenv("K8S_GATEWAY_COMMAND_TIMEOUT", "30"))
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
LOGIN_PROMPT_RE = re.compile(
    r"^(?:Please visit the following URL in your browser manually:\s*|Open\s+)"
    r"(https?://[^\s\"'<>\\]+)(.*)$", re.IGNORECASE
)
CODE_RE = re.compile(r"\b[A-Za-z0-9]{4,8}(?:-[A-Za-z0-9]{4,8})+\b")
ALLOWED_RESOURCES = {"pods", "pvc", "jobs", "vcjobs"}


class GatewayError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class KubernetesGateway:
    def __init__(
        self,
        *,
        credential_root: Path = Path("/run/loom/credentials"),
        kubectl: str = "kubectl",
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        self.credential_root = credential_root
        self.kubeconfig = credential_root / "kubeconfig"
        self.policy_path = credential_root / "policy.json"
        self.kubectl = kubectl
        self.timeout = timeout
        self.home = Path(os.getenv("K8S_GATEWAY_HOME", "/tmp/loom-kube-home"))
        self.home.mkdir(parents=True, exist_ok=True)
        self._auth_lock = threading.Lock()
        self._auth_process: subprocess.Popen[bytes] | None = None
        self._auth_stdout = bytearray()
        self._auth_stderr = bytearray()
        self.policy = self._load_policy()

    def _load_policy(self) -> dict[str, str]:
        try:
            value = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayError("credential_policy_unavailable") from exc
        if not isinstance(value, dict):
            raise GatewayError("credential_policy_invalid")
        namespace = value.get("namespace")
        context = value.get("context")
        if (
            not isinstance(namespace, str)
            or not NAME_RE.fullmatch(namespace)
            or (context is not None and (not isinstance(context, str) or not context))
        ):
            raise GatewayError("credential_policy_invalid")
        return {"namespace": namespace, **({"context": context} if context else {})}

    def _base_args(self) -> list[str]:
        if not self.kubeconfig.is_file() or self.kubeconfig.is_symlink():
            raise GatewayError("credential_profile_unavailable")
        args = [self.kubectl, "--kubeconfig", str(self.kubeconfig)]
        if self.policy.get("context"):
            args.extend(["--context", self.policy["context"]])
        args.extend(["--namespace", self.policy["namespace"]])
        return args

    def _run(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str, bool]:
        if len(args) < 2 or args[0] != self.kubectl:
            raise GatewayError("invalid_kubectl_command")
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "PATH": os.getenv("PATH", ""),
                    "KUBECONFIG": str(self.kubeconfig),
                    "HOME": str(self.home),
                },
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(stdin, timeout=timeout or self.timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                self._kill_group(proc)
                stdout, stderr = proc.communicate()
                timed_out = True
        except OSError as exc:
            raise GatewayError("kubernetes_client_unavailable") from exc
        return (
            int(proc.returncode or 0),
            stdout[:MAX_OUTPUT].decode("utf-8", errors="replace"),
            stderr[:MAX_OUTPUT].decode("utf-8", errors="replace"),
            timed_out,
        )

    @staticmethod
    def _challenge(stdout: str, stderr: str) -> dict[str, Any] | None:
        # Only accept kubelogin's explicit interactive prompt on stderr.
        # URLs in API/network errors and credential JSON are not challenges.
        for line in stderr.splitlines():
            match = LOGIN_PROMPT_RE.fullmatch(line.strip())
            if match is None:
                continue
            uri, instructions = match.groups()
            code = parse_qs(urlparse(uri).query).get("user_code", [None])[0]
            code_match = CODE_RE.search(instructions)
            code = code or (code_match.group(0) if code_match else None)
            if not code:
                continue
            return {
                "status": "auth_required",
                "verification_uri": uri,
                "user_code": code,
                "poll_after_seconds": 5,
                "qr_payload": uri,
            }
        return None

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _probe(self) -> dict[str, Any]:
        with self._auth_lock:
            started = self._auth_process is None
            if self._auth_process is None:
                self._start_auth_process_locked()
            if started:
                for _ in range(10):
                    state = self._auth_state_locked()
                    if self._auth_process is not None and self._auth_process.poll() is not None:
                        return state
                    if "verification_uri" in state or state.get("status") == "authenticated":
                        return state
                    threading.Event().wait(0.05)
            return self._auth_state_locked()

    def _start_auth_process_locked(self) -> None:
        old = self._auth_process
        if old is not None and old.poll() is None:
            return
        if old is not None:
            self._kill_group(old)
        self._auth_stdout.clear()
        self._auth_stderr.clear()
        try:
            process = subprocess.Popen(
                [*self._base_args(), "auth", "can-i", "get", "pods"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "PATH": os.getenv("PATH", ""),
                    "KUBECONFIG": str(self.kubeconfig),
                    "HOME": str(self.home),
                },
                start_new_session=True,
            )
        except OSError as exc:
            raise GatewayError("kubernetes_client_unavailable") from exc
        self._auth_process = process
        for stream, target in (
            (process.stdout, self._auth_stdout),
            (process.stderr, self._auth_stderr),
        ):
            if stream is not None:
                threading.Thread(
                    target=self._read_auth_stream,
                    args=(stream, target),
                    daemon=True,
                ).start()

    @staticmethod
    def _read_auth_stream(stream: Any, target: bytearray) -> None:
        while True:
            chunk = stream.readline()
            if not chunk:
                return
            target.extend(chunk[:MAX_OUTPUT])
            if len(target) > MAX_OUTPUT:
                del target[MAX_OUTPUT:]

    def _auth_state_locked(self) -> dict[str, Any]:
        process = self._auth_process
        if process is None:
            return {"status": "auth_failed", "retryable": True}
        stdout = bytes(self._auth_stdout).decode("utf-8", errors="replace")
        stderr = bytes(self._auth_stderr).decode("utf-8", errors="replace")
        if process.poll() is None:
            challenge = self._challenge(stdout, stderr)
            return challenge or {"status": "auth_required", "poll_after_seconds": 5}
        if process.returncode == 0 and stdout.strip().lower() in {"yes", "true"}:
            return {"status": "authenticated"}
        challenge = self._challenge(stdout, stderr)
        if challenge:
            return challenge
        return {"status": "auth_failed", "retryable": False}

    def _auth_start(self) -> dict[str, Any]:
        with self._auth_lock:
            if self._auth_process is not None and self._auth_process.poll() is None:
                self._kill_group(self._auth_process)
                self._auth_process = None
            self._start_auth_process_locked()
            # Give a device-code plugin a short opportunity to print its
            # challenge, while keeping the HTTP request bounded.
            for _ in range(10):
                state = self._auth_state_locked()
                if state.get("status") != "auth_required" or "verification_uri" in state:
                    return state
                threading.Event().wait(0.05)
            return self._auth_state_locked()

    def close(self) -> None:
        with self._auth_lock:
            process = self._auth_process
            if process is not None and process.poll() is None:
                self._kill_group(process)
            if process is not None:
                with contextlib.suppress(Exception):
                    process.wait(timeout=1)
            self._auth_process = None

    def _require_auth(self) -> dict[str, Any] | None:
        result = self._probe()
        return None if result["status"] == "authenticated" else result

    def _name(self, value: Any) -> str:
        if not isinstance(value, str) or len(value) > 63 or not NAME_RE.fullmatch(value):
            raise GatewayError("invalid_resource_name")
        return value

    def _resource(self, value: Any) -> str:
        if not isinstance(value, str) or value not in ALLOWED_RESOURCES:
            raise GatewayError("resource_kind_not_allowed")
        # Public resource names need not be kubectl short names. Volcano
        # registers vcjob/vj, not vcjobs; use its full API resource to avoid
        # confusing Volcano jobs with native batch/v1 jobs.
        return "jobs.batch.volcano.sh" if value == "vcjobs" else value

    def _manifest(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("apiVersion") != "batch.volcano.sh/v1alpha1" or value.get("kind") != "Job":
            raise GatewayError("volcano_job_required")
        metadata = value.get("metadata")
        if not isinstance(metadata, dict):
            raise GatewayError("volcano_job_metadata_required")
        name = self._name(metadata.get("name"))
        namespace = metadata.get("namespace", self.policy["namespace"])
        if namespace != self.policy["namespace"]:
            raise GatewayError("namespace_forbidden")
        if len(json.dumps(value, separators=(",", ":"))) > MAX_BODY:
            raise GatewayError("request_too_large")
        value = json.loads(json.dumps(value))
        value["metadata"]["name"] = name
        value["metadata"]["namespace"] = self.policy["namespace"]
        return value

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/auth/start":
            return self._auth_start()
        if path == "/v1/auth/status":
            return self._probe()
        auth = self._require_auth()
        if auth:
            return auth
        if path == "/v1/resources/list":
            resource = self._resource(payload.get("resource", "pods"))
            code, stdout, _stderr, timed_out = self._run(
                [*self._base_args(), "get", resource, "-o", "json"]
            )
            if timed_out or code != 0:
                raise GatewayError("kubernetes_request_failed")
            try:
                return {"status": "ok", "resource": json.loads(stdout)}
            except json.JSONDecodeError as exc:
                raise GatewayError("kubernetes_response_invalid") from exc
        if path == "/v1/resources/get":
            resource = self._resource(payload.get("resource", "pods"))
            name = self._name(payload.get("name"))
            code, stdout, _stderr, timed_out = self._run(
                [*self._base_args(), "get", resource, name, "-o", "json"]
            )
            if timed_out or code != 0:
                raise GatewayError("kubernetes_request_failed")
            try:
                return {"status": "ok", "resource": json.loads(stdout)}
            except json.JSONDecodeError as exc:
                raise GatewayError("kubernetes_response_invalid") from exc
        if path == "/v1/jobs/submit":
            manifest = self._manifest(payload.get("manifest"))
            code, stdout, _stderr, timed_out = self._run(
                [*self._base_args(), "apply", "-f", "-"],
                stdin=json.dumps(manifest, separators=(",", ":")).encode(),
            )
            if timed_out or code != 0:
                raise GatewayError("volcano_job_submit_failed")
            return {"status": "submitted", "name": manifest["metadata"]["name"], "message": stdout[-1000:]}
        if path == "/v1/jobs/status":
            name = self._name(payload.get("name"))
            code, stdout, _stderr, timed_out = self._run(
                [*self._base_args(), "get", "vcjob", name, "-o", "json"]
            )
            if timed_out or code != 0:
                raise GatewayError("volcano_job_status_failed")
            try:
                return {"status": "ok", "job": json.loads(stdout)}
            except json.JSONDecodeError as exc:
                raise GatewayError("kubernetes_response_invalid") from exc
        if path == "/v1/jobs/logs":
            pod = self._name(payload.get("pod_name"))
            code, stdout, _stderr, timed_out = self._run(
                [*self._base_args(), "logs", pod, "--tail", "1000"]
            )
            if timed_out or code != 0:
                raise GatewayError("job_logs_failed")
            return {"status": "ok", "pod_name": pod, "logs": stdout[-MAX_OUTPUT:]}
        if path == "/v1/jobs/delete":
            name = self._name(payload.get("name"))
            code, stdout, _stderr, timed_out = self._run(
                [*self._base_args(), "delete", "vcjob", name, "--ignore-not-found"]
            )
            if timed_out or code != 0:
                raise GatewayError("volcano_job_delete_failed")
            return {"status": "deleted", "name": name, "message": stdout[-1000:]}
        raise GatewayError("not_found")


class Handler(BaseHTTPRequestHandler):
    gateway: KubernetesGateway

    def _write(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._write(200, {"status": "ok"})
        else:
            self._write(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise GatewayError("request_too_large")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise GatewayError("request_json_invalid")
            result = self.gateway.handle(self.path, payload)
            self._write(200, result)
        except GatewayError as exc:
            self._write(200, {"status": "error", "error": exc.code})
        except (ValueError, json.JSONDecodeError):
            self._write(200, {"status": "error", "error": "request_json_invalid"})

    def log_message(self, format: str, *_args: Any) -> None:
        return


def main() -> None:
    root = Path(os.getenv("K8S_GATEWAY_CREDENTIAL_ROOT", "/run/loom/credentials"))
    gateway = KubernetesGateway(
        credential_root=root,
        kubectl=os.getenv("K8S_GATEWAY_KUBECTL", "kubectl"),
    )
    Handler.gateway = gateway
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    try:
        server.serve_forever()
    finally:
        gateway.close()
        server.server_close()


if __name__ == "__main__":
    main()
