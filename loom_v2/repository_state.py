from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from datetime import datetime, timezone
from copy import deepcopy
from typing import Any
from loom_v2.contracts.types import CapabilityPackageActivation, CapabilityPackageVersion, ClosureContract, ClosureVersion, DynamicNode, TaskClosure

def execution_transaction(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._execution_transaction(str(args[0]) if args and isinstance(args[0], str) else kwargs.get("run_id")):
            return await method(self, *args, **kwargs)
    return wrapped


def resource_transaction(method):
    """Resource writers lock their own objects, even with a caller's Run context.

    Pool can hold that Run lock while calling Observer over HTTP. Acquiring it
    again in Observer would deadlock the two independent transactions.
    """
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._execution_transaction():
            return await method(self, *args, **kwargs)
    return wrapped


@dataclass
class PatchReceipt:
    receipt: str
    run_id: str
    kind: str
    draft_version: str
    draft_digest: str
    snapshot: TaskClosure
    patch_cursor: int
    readiness: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    run_id: str
    task_ref: str
    goal: str
    draft: ClosureVersion
    closure_contract: ClosureContract | None = None
    allow_reassignment: bool = False
    committed: ClosureVersion | None = None
    execution_id: str | None = None
    execution_epoch: int = 1
    state: str = "opened"
    outcome: dict[str, Any] | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    capability_packages: list[CapabilityPackageVersion] = field(default_factory=list)
    capability_activations: list[CapabilityPackageActivation] = field(default_factory=list)
    dynamic_nodes: list[DynamicNode] = field(default_factory=list)

    @property
    def draft_version(self) -> str:
        return self.draft.version_id

    @property
    def draft_digest(self) -> str:
        return self.draft.snapshot_digest


@dataclass
class RunSummary:
    run_id: str
    state: str
    closure_version: str | None = None
    snapshot_digest: str | None = None
    execution_id: str | None = None
    execution_epoch: int | None = None


@dataclass
class RunCapabilities:
    run_id: str
    workspace_id: str | None
    capability_packages: list[CapabilityPackageVersion]
    capability_activations: list[CapabilityPackageActivation]


@dataclass
class PlanSummary:
    run_id: str
    closure_version: str
    snapshot_digest: str


@dataclass
class ExecutionRequest:
    workspace_id: str
    request_id: str
    request_payload: dict[str, Any]
    run_id: str
    closure_version: str
    closure_contract: dict[str, Any] = field(default_factory=dict)
    snapshot_digest: str = ""
    source: str = "external"
    conversation_ref: str | None = None
    state: str = "queued"
    driver_id: str | None = None
    driver_instance_id: str | None = None
    driver_epoch: int | None = None
    execution_id: str | None = None
    execution_epoch: int | None = None
    outcome: dict[str, Any] | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def model_dump(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "request_id": self.request_id,
            "request_payload": deepcopy(self.request_payload),
            "run_id": self.run_id,
            "closure_version": self.closure_version,
            "source": self.source,
            "closure_contract": deepcopy(self.closure_contract),
            "snapshot_digest": self.snapshot_digest,
            "conversation_ref": self.conversation_ref,
            "state": self.state,
            "driver_id": self.driver_id,
            "driver_instance_id": self.driver_instance_id,
            "driver_epoch": self.driver_epoch,
            "execution_id": self.execution_id,
            "execution_epoch": self.execution_epoch,
            "outcome": deepcopy(self.outcome),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


RUN_TRANSITIONS: dict[str, dict[str, str]] = {
    "opened": {"refinement_started": "thinking", "run_cancelled": "cancelled"},
    "thinking": {"committed": "committed", "run_failed": "failed", "run_cancelled": "cancelled"},
    "committed": {
        "execution_started": "running",
        "run_needs_decision": "awaiting_decision",
        "run_cancelled": "cancelled",
    },
    "running": {
        "run_succeeded": "completed",
        "run_needs_decision": "awaiting_decision",
        "activation_wait": "awaiting_auth",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
    },
    "awaiting_auth": {
        "activation_ready": "running",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
    },
    "awaiting_decision": {
        "decision_accepted": "completed",
        "decision_abandoned": "failed",
        "refinement_started": "thinking",
        "run_cancelled": "cancelled",
    },
    "cleaning_up": {"run_needs_decision": "cleaning_up"},
    "completed": {"run_closed": "closed"},
    "failed": {"run_closed": "closed"},
    "cancelled": {"run_closed": "closed"},
    "closed": {},
}


def _transition(record: RunRecord, event: str, *, reason: Any | None = None) -> str:
    current = record.state
    target = RUN_TRANSITIONS.get(current, {}).get(event)
    if target is None:
        raise ValueError("illegal_state_transition")
    if event == "refinement_started" and current == "awaiting_decision":
        if not isinstance(record.outcome, dict) or record.outcome.get("decision") != "repair":
            raise ValueError("illegal_state_transition")
    if event == "decision_accepted":
        if not isinstance(record.outcome, dict) or record.outcome.get("decision") != "attestation":
            raise ValueError("illegal_state_transition")
    record.state = target
    transition_event: dict[str, Any] = {
        "phase": "run_transition",
        "event": event,
        "from_state": current,
        "to_state": target,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason is not None:
        transition_event["reason"] = deepcopy(reason)
    record.events.append(transition_event)
    return target


def run_record_payload(record: RunRecord, *, include_snapshots: bool = True) -> dict[str, Any]:
    payload = {
        "run_id": record.run_id,
        "task_ref": record.task_ref,
        "goal": record.goal,
        "allow_reassignment": record.allow_reassignment,
        "state": record.state,
        "status": ObserverRepository._conversation_status_for_state(record.state) if "ObserverRepository" in globals() else record.state,
        "draft_version": record.draft.version_id,
        "draft_digest": record.draft.snapshot_digest,
        "closure_contract": record.closure_contract.model_dump(mode="json") if record.closure_contract else None,
        "committed_version": record.committed.version_id if record.committed else None,
        "execution_id": record.execution_id,
        "execution_epoch": record.execution_epoch,
        "outcome": record.outcome,
        "attempts": record.attempts,
        "events": record.events,
        "dynamic_nodes": [node.model_dump(mode="json") for node in record.dynamic_nodes],
        "capability_packages": [package.model_dump(mode="json") for package in record.capability_packages],
        "capability_activations": [activation.model_dump(mode="json") for activation in record.capability_activations],
    }
    if record.state == "awaiting_auth":
        waiting = next((event for event in reversed(record.events) if event.get("phase") == "activation_wait"), None)
        payload["waiting"] = deepcopy((waiting or {}).get("waiting") or {})
    if include_snapshots:
        payload["draft"] = record.draft.model_dump(mode="json")
        payload["committed"] = record.committed.model_dump(mode="json") if record.committed else None
    return payload
