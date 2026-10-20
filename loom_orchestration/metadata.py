"""Machine-readable relationships between capability actions and resources.

The models in this module describe metadata carried by an action descriptor.
They deliberately do not execute a provider action; they only validate the
references and bounded predicates an external planner may use to compose nodes.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import ContractModel, ResourceRef


_PATH = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+|\[\])*$")


class ResourceCondition(ContractModel):
    path: str = Field(min_length=1)
    operator: Literal["equals", "not_equals", "present", "absent"]
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "ResourceCondition":
        if self.operator in {"present", "absent"} and self.value is not None:
            raise ValueError("resource_condition_value_not_allowed")
        if self.operator in {"equals", "not_equals"} and self.value is None:
            raise ValueError("resource_condition_value_required")
        if not _PATH.fullmatch(self.path):
            raise ValueError("resource_condition_path_invalid")
        return self


class ResourcePolling(ContractModel):
    max_attempts: int = Field(ge=1, le=1000)
    interval_seconds: float = Field(gt=0, le=3600)


class ResourceReadiness(ContractModel):
    action_ref: ResourceRef
    result_path: str = Field(min_length=1)
    identity_path: str = Field(min_length=1)
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    conditions: list[ResourceCondition] = Field(default_factory=list, min_length=1)
    polling: ResourcePolling

    @model_validator(mode="after")
    def validate_paths(self) -> "ResourceReadiness":
        if not _PATH.fullmatch(self.result_path) or not _PATH.fullmatch(self.identity_path):
            raise ValueError("resource_readiness_path_invalid")
        validate_mapping(self.input_mapping)
        return self


class ResourceAccessRequirement(ContractModel):
    capability_kind: str = Field(min_length=1)
    package_ref: ResourceRef
    target_binding: Literal["target_resource_ref"] = "target_resource_ref"
    instance_config_mapping: dict[str, Any] = Field(default_factory=dict)
    requires_authentication: bool = False
    ready_state: str = Field(default="ready", min_length=1)

    @model_validator(mode="after")
    def validate_mapping(self) -> "ResourceAccessRequirement":
        validate_mapping(self.instance_config_mapping)
        return self


class ResourceCleanup(ContractModel):
    action_ref: ResourceRef
    input_mapping: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mapping(self) -> "ResourceCleanup":
        validate_mapping(self.input_mapping)
        return self


def validate_mapping(mapping: dict[str, Any]) -> None:
    for value in mapping.values():
        if isinstance(value, str):
            if value != "$resource.identity" and not _PATH.fullmatch(value):
                raise ValueError("resource_mapping_invalid")
            if "[]" in value:
                raise ValueError("resource_mapping_cardinality_unsupported")
        elif isinstance(value, dict):
            if set(value) != {"constant"}:
                raise ValueError("resource_mapping_invalid")
        elif isinstance(value, list):
            validate_mapping(dict(enumerate(value)))
        else:
            raise ValueError("resource_mapping_invalid")


class ResourceEffect(ContractModel):
    effect: Literal["create", "update", "delete"]
    resource_type: str = Field(min_length=1)
    identity_path: str = Field(min_length=1)
    readiness: ResourceReadiness | None = None
    access_requirements: list[ResourceAccessRequirement] = Field(default_factory=list)
    cleanup: ResourceCleanup | None = None

    @model_validator(mode="after")
    def validate_identity_path(self) -> "ResourceEffect":
        if not _PATH.fullmatch(self.identity_path):
            raise ValueError("resource_identity_path_invalid")
        return self


def validate_action_descriptor(payload: Any) -> dict[str, Any]:
    """Validate descriptor resource metadata while preserving descriptor extensions.

    Action descriptors intentionally remain open dictionaries because the
    descriptor registry is runtime-neutral. Validate the version, resource
    identity and standardized optional ``resource_effects`` member here.
    """

    if not isinstance(payload, dict):
        raise ValueError("action_descriptor_invalid")
    if "schema_version" in payload and payload["schema_version"] != "action.v1":
        raise ValueError("action_descriptor_version_invalid")
    if "resource_id" in payload:
        from .grip import uri
        uri(payload["resource_id"])
    effects = payload.get("resource_effects")
    if effects is not None:
        if not isinstance(effects, list) or not effects:
            raise ValueError("resource_effects_invalid")
        try:
            for item in effects:
                ResourceEffect.model_validate(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("resource_effects_invalid") from exc
    return payload


def schema_path_exists(schema: Any, path: str) -> bool:
    """Return whether a restricted metadata path exists in a JSON Schema."""
    return schema_at_path(schema, path) is not None


def schema_at_path(schema: Any, path: str) -> dict[str, Any] | None:
    """Resolve the explicit object/array schema subset used by metadata v1."""
    if not isinstance(schema, dict) or not _PATH.fullmatch(path):
        return None
    current: list[Any] = [schema]
    for token in re.findall(r"[A-Za-z0-9_]+|\[\]", path):
        next_schemas: list[Any] = []
        for candidate in current:
            if not isinstance(candidate, dict):
                continue
            if token == "[]":
                if candidate.get("type") == "array" and isinstance(candidate.get("items"), dict):
                    next_schemas.append(candidate["items"])
                continue
            properties = candidate.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get(token), dict):
                next_schemas.append(properties[token])
        current = next_schemas
        if not current:
            return None
    return current[0]


__all__ = [
    "ResourceAccessRequirement",
    "ResourceCleanup",
    "ResourceCondition",
    "ResourceEffect",
    "ResourcePolling",
    "ResourceReadiness",
    "schema_path_exists",
    "validate_action_descriptor",
]
