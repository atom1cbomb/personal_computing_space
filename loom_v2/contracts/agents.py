from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .types import ContractModel
from loom_orchestration.mcue import ResourceMetadata


DRIVER_COMMANDS = frozenset({
    "run.get", "run.close", "run.cancel", "run.fail", "run.resolve",
    "run.recovery.list", "run.recovery.mark", "run.result",
    "execution.enqueue", "execution.claim_next", "execution.get", "execution.recover",
    "agent_signal.record",
    "capability.list", "capability.get", "capability.activations", "capability.health", "capability.desire",
    "node.accept", "node.dispatch", "node.execute", "node.reassign", "node.result", "node.fail",
    "resource.register",
    "activation.wait",
    "cleanup.register", "cleanup.finish", "resource.register_program", "content.materialize", "activation.ensure_program",
})

# Commands whose optional run_id is admitted before executing their handler.
# Recovery and capability listing enforce their own collection scope.
DRIVER_RUN_SCOPED_COMMANDS = frozenset({
    "run.get", "run.close",
    "run.cancel", "run.fail", "run.resolve", "run.result",
    "execution.enqueue", "execution.claim_next", "execution.get", "execution.recover",
    "agent_signal.record", "capability.health", "capability.desire",
    "node.accept", "node.dispatch", "node.execute", "node.reassign", "node.result", "node.fail",
    "resource.register",
    "activation.wait",
    "cleanup.register", "cleanup.finish", "resource.register_program", "content.materialize", "activation.ensure_program",
})


class AgentRegistration(ContractModel):
    role: Literal["driver", "slave"]
    agent_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    resource_metadata: "ResourceMetadata | None" = None


class AgentLease(ContractModel):
    agent_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    epoch: int
    driver_epoch: int | None = None
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    thread_bindings: list[dict[str, Any]] = Field(default_factory=list)
    recovery_runs: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_driver_epoch(self) -> "AgentLease":
        if self.driver_epoch is None:
            self.driver_epoch = self.epoch
        elif self.driver_epoch != self.epoch:
            raise ValueError("driver_epoch_mismatch")
        return self


class DriverCommand(ContractModel):
    request_id: str = Field(min_length=1)
    driver_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    driver_epoch: int
    command: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class DriverThreadBinding(ContractModel):
    workspace_id: str = Field(min_length=1)
    conversation_ref: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    last_turn_id: str | None = None
    turn_state: Literal[
        "idle",
        "starting",
        "in_progress",
        "completed",
        "interrupted",
        "recovery_pending",
    ] = "idle"
    active_request_id: str | None = None
    driver_epoch: int


class DriverRequestReceipt(ContractModel):
    request_id: str
    command: str
    response: dict[str, Any]
