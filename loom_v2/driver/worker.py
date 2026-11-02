from __future__ import annotations

from typing import Any
from loom_orchestration.grip import Names

import httpx

from loom_v2.contracts.types import (
    CapabilityHealthReport,
    CapabilityDeprovisionCommand,
    CapabilityPackageVersion,
    CapabilityProvisionCommand,
    ComputeBinding,
    ResourceRef,
    TaskClosure,
)
from loom_v2.slave.executor import ExecutionResult
from loom_v2.internal_http import InternalHttpClient
from loom_v2.settings import Settings


class WorkerUnavailableError(RuntimeError):
    pass


class WorkerSession:
    """Direct client for an authenticated Slave execution binding."""

    def __init__(
        self,
        slave_id: str,
        base_url: str = "",
        *,
        timeout: float | None = None,
        action_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        internal_api_secret: str | None = None,
        settings: Settings | None = None,
        control=None,
        execution_binding: dict | None = None,
    ) -> None:
        config = settings or Settings()
        self.execution_binding = execution_binding
        self.names = Names(config.grip_authority, config.workspace_id)
        self.control = control
        self.slave_id = slave_id
        self.base_url = base_url.rstrip("/")
        self.action_timeout = action_timeout if action_timeout is not None else timeout if timeout is not None else config.worker_action_timeout_seconds
        self.timeout = self.action_timeout
        self.transport = transport
        self.internal_api_secret = internal_api_secret if internal_api_secret is not None else config.internal_api_secret
        self._http = InternalHttpClient(timeout=self.action_timeout, transport=transport)

    async def close(self) -> None:
        await self._http.close()

    def _headers(self) -> dict[str, str]:
        if not self.internal_api_secret:
            return {}
        return {"X-Loom-Internal-Token": self.internal_api_secret, "Authorization": f"Bearer {self.internal_api_secret}"}

    async def dispatch(
        self,
        *,
        attempt_id: str,
        execution_id: str,
        execution_epoch: int,
        workspace_id: str,
        instance_name: str = "default",
        action: str,
        payload: dict[str, Any],
        closure: TaskClosure,
        binding: ComputeBinding | None,
        driver_id: str | None = None,
        driver_epoch: int | None = None,
    ) -> ExecutionResult:
        request: dict[str, Any] = {
            "attempt_id": attempt_id,
            "execution_id": execution_id,
            "execution_epoch": execution_epoch,
            "workspace_id": workspace_id,
            "instance_name": instance_name,
            "action": action,
            "closure": closure.model_dump(mode="json"),
            "binding": binding.model_dump(mode="json") if binding is not None else None,
        }
        if driver_id is not None:
            request["driver_id"] = driver_id
        if driver_epoch is not None:
            request["driver_epoch"] = driver_epoch
        if not closure.node_input_bindings:
            request["payload"] = payload
        if self.control is not None:
            request["execution_owner"] = {"instance_id": self.control.instance_id, "lease_id": self.control.lease_id}
        if self.execution_binding is not None:
            from uuid import uuid4
            binding = self.execution_binding
            self._http.transport = self.transport
            response = await self._http.request("POST", binding["endpoint"], headers=self._headers(),
                json={"version": "grip.v1", "request_id": uuid4().hex, "method": "INVOKE",
                      "target": self.names.name("slaves", self.slave_id), "action": self.names.action("execute-attempt"),
                      "auth": {"scheme": "bearer", "credential": self.internal_api_secret},
                      "accepted_protocols": [binding["protocol"]], "input": request}, timeout=self.action_timeout)
            envelope = response.json()
            if envelope.get("kind") != "reply":
                raise RuntimeError(envelope.get("error_code", "worker_dispatch_failed"))
            response = httpx.Response(response.status_code, json=envelope["body"])
        else:
            response = await self._post("/worker/v1/dispatch", request)
        if response.status_code >= 400:
            detail = response.json().get("detail", "worker_dispatch_failed")
            raise RuntimeError(str(detail))
        envelope = response.json()
        report = envelope.get("terminal_report") or {}
        terminal_state = str(report.get("state") or "")
        if envelope.get("accepted") is not True or terminal_state not in {"completed", "failed", "decision_required"}:
            raise RuntimeError("worker_dispatch_not_completed")
        if envelope.get("attempt_id") != attempt_id or report.get("attempt_id") != attempt_id:
            raise RuntimeError("stale_attempt")
        if envelope.get("execution_id") != execution_id or report.get("execution_id") != execution_id:
            raise RuntimeError("stale_execution_id")
        if report.get("execution_epoch") != execution_epoch or envelope.get("execution_epoch") != execution_epoch:
            raise RuntimeError("stale_execution_epoch")
        result = report.get("result") or {}
        return ExecutionResult(
            resource_ref=ResourceRef.model_validate(result["resource_ref"]),
            value=result["value"],
            replay_safety=result.get("replay_safety", "Idempotent"),
            terminal_state=terminal_state,
            terminal_error=report.get("error") or report.get("terminal_error"),
            validation_evidence=report.get("validation_evidence") or [],
            provenance=result.get("provenance") or report.get("provenance"),
        )

    async def provision(
        self,
        *,
        command: CapabilityProvisionCommand,
        package: CapabilityPackageVersion,
        driver_id: str | None = None,
        driver_epoch: int | None = None,
    ) -> CapabilityHealthReport:
        payload: dict[str, Any] = {"command": command.model_dump(mode="json"), "package": package.model_dump(mode="json")}
        if driver_id is not None:
            payload["driver_id"] = driver_id
        if driver_epoch is not None:
            payload["driver_epoch"] = driver_epoch
        response = await self._post("/worker/v1/provision", payload)
        if response.status_code >= 400:
            detail = response.json().get("detail", "worker_provision_failed")
            raise RuntimeError(str(detail))
        return CapabilityHealthReport.model_validate(response.json().get("health_report") or response.json())

    async def deprovision(
        self,
        *,
        command: CapabilityDeprovisionCommand,
        package: CapabilityPackageVersion | None = None,
        driver_id: str | None = None,
        driver_epoch: int | None = None,
    ) -> CapabilityHealthReport:
        payload: dict[str, Any] = {"command": command.model_dump(mode="json")}
        if package is not None:
            payload["package"] = package.model_dump(mode="json")
        if driver_id is not None:
            payload["driver_id"] = driver_id
        if driver_epoch is not None:
            payload["driver_epoch"] = driver_epoch
        response = await self._post("/worker/v1/deprovision", payload)
        if response.status_code >= 400:
            detail = response.json().get("detail", "worker_deprovision_failed")
            raise RuntimeError(str(detail))
        return CapabilityHealthReport.model_validate(response.json().get("health_report") or response.json())

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            self._http.transport = self.transport
            if not self.base_url:
                raise WorkerUnavailableError("worker_transport_not_configured")
            return await self._http.request(
                "POST",
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers(),
                timeout=self.action_timeout,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise WorkerUnavailableError("worker_action_timeout") from exc
        except httpx.TransportError as exc:
            raise WorkerUnavailableError("worker_unavailable") from exc
