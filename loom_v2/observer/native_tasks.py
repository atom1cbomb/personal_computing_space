"""GRIP projections and actions for the existing native execution lifecycle."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, RootModel

from loom_orchestration.grip import Model, URI
from loom_v2.contracts.types import ClosureContract
from loom_v2.contracts.errors import DomainError, DomainErrorEnvelope
from .repository import run_record_payload


class SubmitPlanInput(Model):
    plan: ClosureContract
    idempotency_key: str = Field(min_length=1, max_length=256)
    conversation_ref: str | None = Field(default=None, min_length=1, max_length=256)


class SubmissionLookupInput(Model):
    idempotency_key: str = Field(min_length=1, max_length=256)


class ResubmitPlanInput(Model):
    plan: ClosureContract
    idempotency_key: str = Field(min_length=1, max_length=256)
    base_closure_version: str = Field(min_length=1, max_length=128)


class CancelRunInput(Model):
    reason: str | dict[str, Any] = "user_cancel"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class ResolveRunInput(Model):
    decision: Literal["accept", "abandon"]


class CloseRunInput(Model):
    pass


class RunReadInput(Model):
    include_history: bool = False


class TaskOutput(Model):
    run: URI


class PlanRejectedOutput(Model):
    rejected: DomainErrorEnvelope


SubmissionResult = RootModel[TaskOutput | PlanRejectedOutput]


class RunReadOutput(Model):
    # Explicit history requests include the existing Run view's evidence fields.
    model_config = ConfigDict(extra="allow", strict=True)
    run: URI
    run_id: str
    state: str
    outcome: dict[str, Any] | None
    driver: URI
    plan: URI | None
    closure_version: str | None
    snapshot_digest: str | None
    execution_id: str | None
    execution_epoch: int


class PlanReadOutput(Model):
    run: URI
    closure_version: str
    snapshot_digest: str
    plan: ClosureContract


class SubmissionOutput(Model):
    run: URI
    plan: URI | None
    request_id: str
    run_id: str
    closure_version: str
    snapshot_digest: str
    closure_contract: dict[str, Any]
    current_run_state: str
    state: str
    execution_request_state: str
    source: str
    conversation_ref: str | None
    execution_id: str | None
    execution_epoch: int | None
    started_at: str | None
    deadline_at: str | None
    outcome: dict[str, Any] | None
    waiting: dict[str, Any] | None = None


INPUTS = {
    "submit-plan": SubmitPlanInput, "get-submission": SubmissionLookupInput,
    "resubmit": ResubmitPlanInput, "cancel": CancelRunInput,
    "resolve": ResolveRunInput, "close": CloseRunInput,
}
OUTPUTS = {
    "submit-plan": SubmissionResult, "get-submission": SubmissionOutput, "resubmit": SubmissionResult,
    "cancel": RunReadOutput, "resolve": RunReadOutput, "close": RunReadOutput,
}
DESCRIPTIONS = {
    "submit-plan": "Validate and freeze a complete ClosureContract, enqueue it idempotently, and return its Run URI.",
    "get-submission": "Find a durable submission by its business idempotency_key, including after a lost reply.",
    "resubmit": "Replace a repairable Run's plan using base_closure_version and a new idempotency_key.",
    "cancel": "Request cancellation; optionally check the submission idempotency_key belongs to this Run.",
    "resolve": "Accept an attestation or abandon a Run awaiting a decision.",
    "close": "Close a completed, failed or cancelled Run; a repeated close returns its closed state.",
}
USAGE = {
    "computing/driver": (
        "Submit a complete ClosureContract with submit-plan, plan and a business idempotency_key. "
        "Read the input schema for the supported task format. The key is independent of the GRIP message request_id. "
        "Reuse the same key and input to find the same Run; changed input is rejected with request_id_reused. "
        "get-submission looks up that key after a lost reply. Observer validates, freezes and queues the plan; "
        "the returned Run URI confirms durable acceptance, not execution. "
        "A readiness rejection returns rejected with its public code and structured details.blockers; no Run is queued. "
        "accepting_submissions describes the Observer entry point; available describes the Driver lease. Offline Drivers do not prevent queueing. "
        "The single logical Driver executes ordinary plans and container:python_orchestrator/1 orchestration "
        "and keeps this URI across process restarts. Read the Run for results and its plan URI for the frozen definition."
    ),
    "execution/plan": (
        "Read this immutable ClosureContract, its closure_version and snapshot_digest. "
        "The digest identifies the TaskClosure body. The linked Run holds execution state. "
        "Repair creates another plan URI while this URI continues to identify the original definition."
    ),
    "execution/run": (
        "read returns current state, outcome, execution identity and the frozen plan URI. "
        "Use include_history=true to inspect attempts, events, dynamic nodes and evidence. "
        "cancel requests cancellation; cleanup may continue before the Run settles. "
        "resubmit requires awaiting_decision with decision=repair, the current base_closure_version, "
        "a replacement plan and a new idempotency_key. It preserves the Run URI and existing success and policy constraints. "
        "resolve accepts decision=accept for an attestation or decision=abandon for a pending decision. "
        "close requires completed, failed or cancelled; repeated close is idempotent. "
        "Lifecycle actions remain discoverable for retries; the server validates their state conditions."
    ),
}
PUBLIC_ERRORS = {
    "request_id_reused", "workspace_binding_mismatch", "request_run_mismatch", "version_conflict",
    "resubmit_not_repairable", "planning_source_mismatch", "success_contract_changed",
    "constraints_changed", "execution_policy_changed", "closure_goal_mismatch",
    "run_not_terminal", "illegal_state_transition", "invalid_decision", "readiness_blocked",
}


class NativeTasks:
    def __init__(self, grip):
        self.grip = grip
        self.names = grip.names
        self.workspace = grip.settings.workspace_id
        self.driver_uri = self.names.name("drivers", "default")

    @property
    def repo(self):
        return self.grip.repo

    def plan_uri(self, run_id: str, version: str) -> str:
        return self.names.name("plans", run_id, version)

    def run_metadata(self, summary, *, has_plan: bool) -> dict[str, Any]:
        return {
            "state": summary.state, "driver": self.driver_uri,
            "plan": self.plan_uri(summary.run_id, summary.closure_version) if has_plan else None,
            "closure_version": summary.closure_version, "snapshot_digest": summary.snapshot_digest,
            "execution_id": summary.execution_id, "execution_epoch": summary.execution_epoch,
        }

    async def driver_metadata(self) -> dict[str, Any]:
        agents = await self.repo.list_agents(self.workspace, role="driver")
        active = next((a for a in agents if a["lease_state"] == "active"), None)
        return {
            "accepting_submissions": True, "available": active is not None,
            "lease_state": "active" if active else "offline",
            "agent_id": active["agent_id"] if active else None,
            "instance_id": active["instance_id"] if active else None,
            "epoch": active["epoch"] if active else None,
            "last_seen_at": active.get("last_seen_at") if active else None,
            "task_format": {"name": "ClosureContract", "body_schema_version": "1",
                            "schema": self.names.schema("closure-contract")},
            "execution_kinds": ["ordinary-plan", "container:python_orchestrator/1"],
        }

    async def read_run(self, run_id: str, *, include_history=False) -> dict[str, Any]:
        if include_history:
            async with self.repo._execution_transaction():
                record = await self.repo._assert_run_workspace(run_id, self.workspace)
                value = run_record_payload(record, include_snapshots=False)
                value.update(closure_version=record.committed.version_id if record.committed else None,
                             snapshot_digest=record.committed.snapshot_digest if record.committed else None)
        else:
            value = await self.repo.read_run_result(self.workspace, run_id)
        plans = (await self.repo.list_plan_summaries(self.workspace, run_id=run_id, closure_version=value["closure_version"])
                 if value["closure_version"] else [])
        return {**value, "run": self.names.name("runs", run_id), "driver": self.driver_uri,
                "plan": self.plan_uri(run_id, value["closure_version"]) if plans else None}

    async def submission(self, key: str) -> dict[str, Any]:
        # Refresh both records in one snapshot; the ordinary getters may cache
        # rows while another Observer process records progress.
        async with self.repo._execution_transaction():
            item = await self.repo.get_execution_request(self.workspace, key)
            run = await self.repo._assert_run_workspace(item.run_id, self.workspace)
        payload = {
            "run": self.names.name("runs", item.run_id),
            "plan": self.plan_uri(item.run_id, item.closure_version) if item.snapshot_digest else None,
            "request_id": item.request_id, "run_id": item.run_id, "closure_version": item.closure_version,
            "snapshot_digest": item.snapshot_digest, "closure_contract": item.closure_contract,
            "current_run_state": run.state,
            "state": (item.outcome or {}).get("disposition", run.state) if item.state == "settled" else run.state,
            "execution_request_state": item.state, "source": item.source,
            "conversation_ref": item.conversation_ref, "execution_id": item.execution_id,
            "execution_epoch": item.execution_epoch,
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "deadline_at": item.deadline_at.isoformat() if item.deadline_at else None, "outcome": item.outcome,
        }
        if run.state == "awaiting_auth":
            payload["waiting"] = run_record_payload(run, include_snapshots=False).get("waiting", {})
        return payload

    async def invoke(self, action: str, data: dict, *, run_id: str | None = None) -> dict:
        try:
            return await self._invoke(action, data, run_id=run_id)
        except DomainError as exc:
            if action in {"submit-plan", "resubmit"} and exc.envelope.code == "readiness_blocked":
                # Domain rejection is an action result. Keep the strict GRIP v1
                # error envelope unchanged and preserve the public diagnostics.
                return {"rejected": exc.envelope.model_dump(mode="json")}
            raise

    async def _invoke(self, action: str, data: dict, *, run_id: str | None = None) -> dict:
        if action == "submit-plan":
            payload = SubmitPlanInput.model_validate(data)
            item = await self.repo.submit_run_plan(workspace_id=self.workspace, request_id=payload.idempotency_key,
                plan=payload.plan, conversation_ref=payload.conversation_ref, source="external")
            return {"run": self.names.name("runs", item.run_id)}
        if action == "get-submission":
            return await self.submission(SubmissionLookupInput.model_validate(data).idempotency_key)
        if action == "resubmit":
            payload = ResubmitPlanInput.model_validate(data)
            item = await self.repo.resubmit_run_plan(workspace_id=self.workspace, run_id=run_id,
                request_id=payload.idempotency_key, base_closure_version=payload.base_closure_version, plan=payload.plan)
            return {"run": self.names.name("runs", item.run_id)}
        async with self.repo._execution_transaction():
            record = await self.repo._assert_run_workspace(run_id, self.workspace)
            if action == "cancel":
                payload = CancelRunInput.model_validate(data)
                if payload.idempotency_key:
                    item = await self.repo.get_execution_request(self.workspace, payload.idempotency_key)
                    if item.run_id != run_id:
                        raise ValueError("request_run_mismatch")
                await self.repo.cancel_run(run_id, payload.reason)
            elif action == "resolve":
                await self.repo.resolve_run(run_id, ResolveRunInput.model_validate(data).decision)
            elif action == "close":
                if record.state != "closed":
                    await self.repo.close_run(run_id)
            else:
                raise ValueError("unknown_native_action")
        return await self.read_run(run_id)
