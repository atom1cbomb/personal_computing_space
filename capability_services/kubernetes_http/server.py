"""Small, policy-bound HTTP gateway for a user's Kubernetes namespace.

The image intentionally uses the kubectl CLI because the supported cluster
authentication is the kubeconfig exec plugin from production/kubernetes.md.
Only fixed actions are exposed; request data is never interpolated into a
shell command.
"""

from __future__ import annotations

import json
import errno
import logging
import os
import re
import subprocess
import signal
import threading
import time
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from typing import Any
from fastapi import Request, WebSocket


MAX_BODY = 2 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
COMMAND_TIMEOUT = float(os.getenv("K8S_GATEWAY_COMMAND_TIMEOUT", "30"))
AUTH_CHECK_INTERVAL = 30.0
AUTH_RETRY_INITIAL = 2.0
AUTH_RETRY_MAX = 60.0
AUTH_LOGIN_TIMEOUT = 300.0
logger = logging.getLogger(__name__)
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
        self.home = Path(os.getenv("K8S_GATEWAY_HOME", "/var/lib/loom/service/kube"))
        self.home.mkdir(parents=True, exist_ok=True)
        self._auth_lock = threading.Lock()
        self._auth_process: subprocess.Popen[bytes] | None = None
        self._auth_stdout = bytearray()
        self._auth_stderr = bytearray()
        self._auth_readers: list[threading.Thread] = []
        self._auth_result: dict[str, Any] | None = None
        self._auth_next_retry_at: float | None = None
        self._auth_failures = 0
        self._auth_deadline: float | None = None
        self._auth_pending_status = "auth_required"
        self._auth_challenge_seen = False
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
            if self._auth_process is None and self._auth_result is None:
                self._start_auth_process_locked()
                return self._wait_auth_start_locked()
            state = self._auth_state_locked()
            if self._auth_next_retry_at is not None and time.monotonic() >= self._auth_next_retry_at:
                # Only a previously verified success gets a bounded grace
                # period. Retrying a failure never makes the instance ready.
                self._start_auth_process_locked(pending_status=state["status"])
            return self._auth_state_locked()

    def _stop_auth_process_locked(self) -> None:
        if self._auth_process is not None and self._auth_result is None:
            # Reap the child and terminate helpers even if kubectl itself
            # already exited but a helper still holds its pipes open.
            self._kill_group(self._auth_process)
            self._auth_process.wait(timeout=2)
        for reader in self._auth_readers:
            reader.join(timeout=0.1)

    def _finish_auth_locked(self, result: dict[str, Any]) -> dict[str, Any]:
        self._stop_auth_process_locked()
        self._auth_deadline = None
        self._auth_result = result
        self._auth_next_retry_at = None
        delay = None
        if result["status"] == "authenticated":
            self._auth_failures = 0
            delay = AUTH_CHECK_INTERVAL
        elif result.get("retryable"):
            self._auth_failures = min(self._auth_failures + 1, 7)
            delay = min(AUTH_RETRY_INITIAL * 2 ** (self._auth_failures - 1), AUTH_RETRY_MAX)
        else:
            self._auth_failures = 0
        if delay is not None:
            self._auth_next_retry_at = time.monotonic() + delay
        if result["status"] == "auth_failed":
            # Fixed reason codes only: stderr can contain provider credentials.
            logger.warning("Kubernetes authentication check failed: reason=%s retry_in=%s",
                           result["reason"], delay)
        return dict(result)

    def _start_auth_process_locked(self, *, pending_status: str = "auth_required") -> None:
        self._stop_auth_process_locked()
        self._auth_process = None
        # Old reader threads retain their old buffers and cannot contaminate
        # another attempt's challenge or authorization result.
        self._auth_stdout, self._auth_stderr = bytearray(), bytearray()
        self._auth_readers = []
        self._auth_result = None
        self._auth_next_retry_at = None
        self._auth_pending_status = pending_status
        self._auth_challenge_seen = False
        self._auth_deadline = time.monotonic() + self.timeout
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
        except (OSError, GatewayError) as exc:
            retryable = isinstance(exc, OSError) and exc.errno not in {errno.ENOENT, errno.EACCES, errno.ENOEXEC, errno.ENOTDIR}
            self._finish_auth_locked({"status": "auth_failed", "retryable": retryable,
                                      "reason": "probe_failed" if retryable else "configuration_error"})
            return
        self._auth_process = process
        for stream, target in (
            (process.stdout, self._auth_stdout),
            (process.stderr, self._auth_stderr),
        ):
            if stream is not None:
                reader = threading.Thread(
                    target=self._read_auth_stream,
                    args=(stream, target),
                    daemon=True,
                )
                self._auth_readers.append(reader)
                reader.start()

    @staticmethod
    def _read_auth_stream(stream: Any, target: bytearray) -> None:
        try:
            while chunk := stream.readline(65536):
                target.extend(chunk[:MAX_OUTPUT])
                if len(target) > MAX_OUTPUT:
                    del target[MAX_OUTPUT:]
        finally:
            stream.close()

    @staticmethod
    def _auth_failure(stdout: str, stderr: str) -> dict[str, Any]:
        message = stderr.lower()
        if stdout.strip().lower() in {"no", "false"} or "forbidden" in message:
            return {"status": "auth_failed", "retryable": False, "reason": "permission_denied"}
        if any(marker in message for marker in (
            "invalid_client", "invalid_scope", "unsupported_grant_type", "x509:",
            "certificate verify failed", "invalid apiversion", "no configuration has been provided",
            "interactivemode must be specified", "no such file or directory",
        )):
            return {"status": "auth_failed", "retryable": False, "reason": "configuration_error"}
        if any(marker in message for marker in (
            "invalid_grant", "unauthorized", "you must be logged in", "provide credentials",
        )):
            return {"status": "auth_required", "poll_after_seconds": 5}
        transient = any(marker in message for marker in (
            "timeout", "timed out", "deadline exceeded", "context canceled", "connection",
            "eof", "no route", "no such host", "temporary failure", "network",
            "service unavailable", "bad gateway", "too many requests",
        ))
        # Unknown failed checks also get bounded retries; a new provider error
        # must not recreate the permanently latched failure this fixes.
        return {"status": "auth_failed", "retryable": True,
                "reason": "transport_error" if transient else "probe_failed"}

    def _auth_state_locked(self) -> dict[str, Any]:
        if self._auth_result is not None:
            return dict(self._auth_result)
        process = self._auth_process
        if process is None:
            return {"status": "auth_failed", "retryable": True, "reason": "probe_pending"}
        returncode = process.poll()
        if returncode is not None:
            for reader in self._auth_readers:
                reader.join(timeout=0.1)
        stdout = bytes(self._auth_stdout).decode("utf-8", errors="replace")
        stderr = bytes(self._auth_stderr).decode("utf-8", errors="replace")
        challenge = self._challenge(stdout, stderr)
        if returncode is None:
            if challenge and not self._auth_challenge_seen:
                self._auth_challenge_seen = True
                self._auth_pending_status = "auth_required"
                self._auth_deadline = time.monotonic() + AUTH_LOGIN_TIMEOUT
            if self._auth_deadline is not None and time.monotonic() >= self._auth_deadline:
                result = ({"status": "auth_required", "poll_after_seconds": 5} if self._auth_challenge_seen else
                          {"status": "auth_failed", "retryable": True, "reason": "probe_timeout"})
                return self._finish_auth_locked(result)
            if challenge:
                return challenge
            if self._auth_pending_status == "authenticated":
                return {"status": "authenticated"}
            if self._auth_pending_status == "auth_failed":
                return {"status": "auth_failed", "retryable": True, "reason": "probe_pending"}
            return {"status": "auth_required", "poll_after_seconds": 5}
        if returncode == 0 and stdout.strip().lower() in {"yes", "true"}:
            return self._finish_auth_locked({"status": "authenticated"})
        if challenge:
            # The process no longer polls the authorization server. The printed
            # device URL/code belongs to a dead attempt and must not be reused.
            return self._finish_auth_locked({"status": "auth_required", "poll_after_seconds": 5})
        return self._finish_auth_locked(self._auth_failure(stdout, stderr))

    def _wait_auth_start_locked(self) -> dict[str, Any]:
        # Give a fast credential check/device prompt a short opportunity to
        # finish without making health requests wait for upstream networking.
        for _ in range(10):
            state = self._auth_state_locked()
            if self._auth_result is not None or "verification_uri" in state or state["status"] == "authenticated":
                return state
            threading.Event().wait(0.05)
        return self._auth_state_locked()

    def _auth_start(self) -> dict[str, Any]:
        with self._auth_lock:
            self._auth_failures = 0
            self._start_auth_process_locked()
            return self._wait_auth_start_locked()

    def close(self) -> None:
        with self._auth_lock:
            self._stop_auth_process_locked()
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


def create_app(gateway, access=None):
    import asyncio
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from loom_orchestration.stream_server import serve_stream
    from .service_access import AccessError, ServiceAccess
    access = access or ServiceAccess(gateway)

    @asynccontextmanager
    async def lifespan(app):
        await access.start_saved()
        try:
            yield
        finally:
            await access.close()
            gateway.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.websocket("/v1/services/stream")
    async def stream(websocket: WebSocket):
        await serve_stream(websocket, access.open)

    @app.post("/v1/{path:path}")
    async def invoke(path: str, request: Request):
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise GatewayError("request_too_large")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise GatewayError("request_json_invalid")
            if path == "services/ensure":
                return await access.ensure(payload.get("service_ref"))
            if path == "services/access-status":
                return access.status(payload.get("service_ref"))
            return await asyncio.to_thread(gateway.handle, "/v1/" + path, payload)
        except (GatewayError, AccessError) as exc:
            return JSONResponse({"status": "error", "error": exc.code})
        except (ValueError, TypeError):
            return JSONResponse({"status": "error", "error": "request_json_invalid"})
    return app


def main():
    import uvicorn
    gateway = KubernetesGateway(credential_root=Path(os.getenv("K8S_GATEWAY_CREDENTIAL_ROOT", "/run/loom/credentials")),
        kubectl=os.getenv("K8S_GATEWAY_KUBECTL", "kubectl"))
    uvicorn.run(create_app(gateway), host="0.0.0.0", port=int(os.getenv("PORT", "8080")),
        ws_max_size=65536, ws_max_queue=16, ws_ping_interval=20, ws_ping_timeout=40, access_log=False)


if __name__ == "__main__":
    main()
