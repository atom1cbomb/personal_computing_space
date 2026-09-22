"""Pydantic models for service-to-service HTTP request envelopes.

These models intentionally stop at the HTTP boundary.  They validate envelope
shape and nested contract models; workspace, lease, fencing, content and
runtime admission rules remain in the owning service.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .types import (
    CapabilityExport,
    CapabilityDeprovisionCommand,
    CapabilityHealthReport,
    CapabilityPackageVersion,
    CapabilityProvisionCommand,
    ComputeBinding,
    ContractModel,
    ClosureContract,
    ExecutionContract,
    ResourceRef,
    TaskClosure,
)
from .browser_auth import BrowserAuthContext, BrowserAuthMessage


class AgentLeaseRequest(ContractModel):
    """Heartbeat/release envelope shared by Driver and Slave agents."""

    instance_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    epoch: int | None = None
    driver_epoch: int | None = None
    role: Literal["driver", "slave"] = "driver"


class PublicRequest(BaseModel):
    """Stable public envelope with the API's historical extra-field behavior."""

    model_config = ConfigDict(extra="ignore")


class PublicSlave(BaseModel):
    """Workspace resource discovery, without internal routing or lease tokens."""

    slave_id: str
    available: bool
    lease_state: str
    last_seen_at: str | None = None
    base_operations: list[str] = Field(default_factory=list)
    executor_descriptors: list[dict[str, Any]] = Field(default_factory=list)
    runtime_plugin_descriptors: list[dict[str, Any]] = Field(default_factory=list)
    term_support: list[dict[str, Any]] = Field(default_factory=list)


class PublicSlaveList(BaseModel):
    workspace_id: str
    slaves: list[PublicSlave]


class ContentPutRequest(PublicRequest):
    content: Any | None = None
    media_type: Any = ""


class CapabilityPackageUploadRequest(PublicRequest):
    """Manifest for a package supplied directly to a workspace."""

    package_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    package_type: str = Field(default="function", min_length=1)
    execution: ExecutionContract = Field(default_factory=ExecutionContract)
    capability_exports: list[CapabilityExport] = Field(min_length=1)
    body: dict[str, Any] | None = None
    package_digest: str | None = None


class ResourceCreateRequest(PublicRequest):
    resource_ref: ResourceRef
    package_ref: ResourceRef
    managed_by_ref: ResourceRef | None = None
    state: str = "unknown"


class OpenRunRequest(PublicRequest):
    run_id: str | None = None
    task_ref: str | None = None
    goal: str | None = None
    closure_contract: ClosureContract | None = None
    allow_reassignment: bool = False
    user_id: str | None = None
    workspace_id: str | None = None


class PublicMessageRequest(PublicRequest):
    # Preserve the endpoint's domain-specific validation codes for missing,
    # blank, or wrongly typed values instead of replacing them with FastAPI's
    # generic request-validation envelope.
    text: Any = None
    request_id: Any = None
    conversation_ref: Any = None
    workspace_id: str | None = None


class PatchRequest(PublicRequest):
    base_draft_version: str | None = None
    base_snapshot_digest: str | None = None
    operation_id: str | None = None
    ops: list[dict[str, Any]] = Field(default_factory=list)


class CommitRequest(PublicRequest):
    draft_version: str | None = None
    draft_digest: str | None = None


class StartRequest(PublicRequest):
    closure_version: str | None = None


class ResolveRequest(PublicRequest):
    decision: str | None = None


class IdempotencyRequest(PublicRequest):
    idempotency_key: str | None = None


class CapabilityActivationRequest(PublicRequest):
    target_slaves: list[str] = Field(default_factory=list)
    approved_digest: str | None = None
    idempotency_key: str | None = None
    compute_binding: ComputeBinding | None = None
    instance_name: str = Field(default="default", pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    target_resource_ref: ResourceRef | None = None
    instance_config: dict[str, Any] = Field(default_factory=dict)


class CapabilityHealthRequest(ContractModel):
    """Lease-authenticated health report sent by a Slave."""

    agent_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    epoch: int
    workspace_id: str = Field(min_length=1)
    report: CapabilityHealthReport


class DriverMessageRequest(ContractModel):
    text: Any = None
    request_id: Any = None
    conversation_ref: Any = None
    workspace_id: str | None = None


class WorkspaceRequest(ContractModel):
    workspace_id: str | None = None


class SlaveDispatchRequest(ContractModel):
    attempt_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    execution_epoch: int = 1
    workspace_id: str | None = None
    instance_name: str = Field(default="default", pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    operation: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    closure: TaskClosure | None = None
    binding: ComputeBinding | None = None
    driver_id: str | None = None
    driver_epoch: int | None = None


class SlaveProvisionRequest(ContractModel):
    command: CapabilityProvisionCommand
    package: CapabilityPackageVersion
    driver_id: str | None = None
    driver_epoch: int | None = None


class SlaveDeprovisionRequest(ContractModel):
    command: CapabilityDeprovisionCommand
    package: CapabilityPackageVersion | None = None
    driver_id: str | None = None
    driver_epoch: int | None = None


class SlaveAuthStartRequest(ContractModel):
    """Request to restart authentication for an existing activation."""

    package: CapabilityPackageVersion
    target_slave: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    driver_id: str | None = None
    driver_epoch: int | None = None
    instance_name: str = Field(default="default", pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    browser_auth: BrowserAuthContext | None = None
    activation_revision: int | None = Field(default=None, ge=1)


class SlaveBrowserRequest(SlaveAuthStartRequest):
    activation_revision: int = Field(ge=1)
    payload: BrowserAuthMessage


__all__ = [
    "AgentLeaseRequest",
    "CapabilityHealthRequest",
    "CapabilityActivationRequest",
    "CapabilityPackageUploadRequest",
    "ResourceCreateRequest",
    "CommitRequest",
    "ContentPutRequest",
    "DriverMessageRequest",
    "IdempotencyRequest",
    "OpenRunRequest",
    "PatchRequest",
    "PublicMessageRequest",
    "ResolveRequest",
    "SlaveDeprovisionRequest",
    "SlaveAuthStartRequest",
    "SlaveDispatchRequest",
    "SlaveProvisionRequest",
    "StartRequest",
    "WorkspaceRequest",
]
