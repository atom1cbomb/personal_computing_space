"""Small, dependency-light versions of the public submission contracts."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceRef(ContractModel):
    resource_id: str
    version_or_digest: str | None = None
    access_binding: dict[str, Any] = Field(default_factory=dict)
    identity_criterion: str | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity(self) -> "ResourceRef":
        if self.resource_id.startswith("content://sha256/") and self.version_or_digest is not None:
            raise ValueError("redundant_content_digest")
        return self

    @property
    def digest(self) -> str | None:
        if self.identity_criterion == "descriptor_digest" and self.version_or_digest:
            return self.version_or_digest.lower()
        if self.resource_id.startswith("content://sha256/"):
            return self.resource_id.removeprefix("content://sha256/").lower()
        return self.version_or_digest.lower() if self.version_or_digest else None


class TaskClosure(ContractModel):
    closure_id: str = "closure-default"
    schema_version: str = "1"
    data: dict[str, Any] = Field(default_factory=dict)
    data_systems: dict[str, Any] = Field(default_factory=dict)
    program: dict[str, Any] = Field(default_factory=dict)
    program_systems: dict[str, Any] = Field(default_factory=dict)
    node_input_bindings: list[dict[str, Any]] = Field(default_factory=list)
    compute: dict[str, Any] = Field(default_factory=dict)
    compute_bindings: list[dict[str, Any]] = Field(default_factory=list)
    compute_application: dict[str, Any] = Field(default_factory=dict)
    compute_systems: dict[str, Any] = Field(default_factory=dict)
    terms: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClosureContract(ContractModel):
    closure_id: str
    goal: str
    origin_conversation_ref: str = ""
    user_id: str = "user-default"
    workspace_id: str = "workspace-default"
    required_success_criteria: list[dict[str, Any]] = Field(default_factory=list)
    allowed_effects: list[str] = Field(default_factory=list)
    resource_budget: dict[str, Any] = Field(default_factory=dict)
    recovery_policy: dict[str, Any] = Field(default_factory=dict)
    result_expectations: list[dict[str, Any]] = Field(default_factory=list)
    declared_constraints: list[dict[str, Any]] = Field(default_factory=list)
    body: TaskClosure | dict[str, Any]


class Submission(ContractModel):
    request_id: str
    conversation_ref: str | None = None
    plan: ClosureContract


def ensure_sha256(value: ResourceRef, *, descriptor: bool = False) -> ResourceRef:
    digest = value.digest or ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest, re.IGNORECASE):
        raise ValueError("resource_digest_required")
    if descriptor and value.identity_criterion != "descriptor_digest":
        raise ValueError("descriptor_digest_required")
    return value


__all__ = ["ClosureContract", "ResourceRef", "Submission", "TaskClosure", "ensure_sha256"]
