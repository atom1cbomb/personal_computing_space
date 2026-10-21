"""Validate resource relationships without generating an execution plan."""

from __future__ import annotations

from typing import Any
from copy import deepcopy

from jsonschema import Draft202012Validator

from loom_orchestration.metadata import ResourceEffect, schema_at_path
from loom_v2.contracts.types import IoContract, ResourceRef
from loom_v2.contracts.io_schema import validate_schema


async def activation_config_schema(repository: Any, package: Any) -> dict[str, Any] | None:
    refs = []
    for export in package.action_exports:
        descriptor = await repository.get_action_descriptor(export.action_descriptor_ref)
        if descriptor.get("instance_config_schema_ref"):
            refs.append(ResourceRef.model_validate(descriptor["instance_config_schema_ref"]))
    if not refs:
        return None
    if len({ref.digest for ref in refs}) != 1:
        raise ValueError("activation_config_schema_conflict")
    schema = await repository._load_json_content(refs[0])
    validate_schema(schema)
    if schema.get("type") != "object":
        raise ValueError("activation_config_schema_invalid")
    return schema


async def validate_activation_config(repository: Any, package: Any, config: dict[str, Any]) -> None:
    schema = await activation_config_schema(repository, package)
    if schema is not None:
        if set(config) - set(schema.get("properties", {})):
            raise ValueError("activation_config_field_unknown")
        if not Draft202012Validator(schema).is_valid(config):
            raise ValueError("activation_config_invalid")


def _at(schema: dict[str, Any], path: str) -> dict[str, Any]:
    result = schema_at_path(schema, path)
    if result is None:
        raise ValueError(f"resource_effect_schema_path_unavailable:{path}")
    return result


def _types(schema: dict[str, Any]) -> set[str]:
    value = schema.get("type")
    if not isinstance(value, (str, list)):
        raise ValueError("resource_effect_schema_unsupported")
    return {value} if isinstance(value, str) else set(value)


def _assignable(source: dict[str, Any], destination: dict[str, Any]) -> None:
    if not _types(source) <= _types(destination):
        raise ValueError("resource_effect_mapping_type_mismatch")
    supported = {"type", "title", "description", "$schema", "$id", "default", "examples"}
    if set(destination) - supported:
        raise ValueError("resource_effect_schema_unsupported")


def _mapping_value(
    value: Any, destination: dict[str, Any], source: dict[str, Any], identity: dict[str, Any]
) -> None:
    if isinstance(value, dict) and set(value) == {"constant"}:
        if not Draft202012Validator(destination).is_valid(value["constant"]):
            raise ValueError("resource_effect_mapping_constant_invalid")
    elif isinstance(value, list):
        if destination.get("type") != "array" or not isinstance(destination.get("items"), dict):
            raise ValueError("resource_effect_mapping_type_mismatch")
        if len(value) < destination.get("minItems", 0) or len(value) > destination.get("maxItems", len(value)):
            raise ValueError("resource_effect_mapping_cardinality_invalid")
        if set(destination) - {"type", "items", "minItems", "maxItems", "description", "title"}:
            raise ValueError("resource_effect_schema_unsupported")
        for item in value:
            _mapping_value(item, destination["items"], source, identity)
    elif isinstance(value, str):
        _assignable(identity if value == "$resource.identity" else _at(source, value), destination)
    else:
        raise ValueError("resource_effect_mapping_invalid")


def _mapping(
    mapping: dict[str, Any], destination: dict[str, Any], source: dict[str, Any], identity: dict[str, Any]
) -> None:
    if destination.get("type") != "object":
        raise ValueError("resource_effect_mapping_schema_required")
    if set(destination) - {"type", "properties", "required", "additionalProperties", "$schema", "$id", "title", "description"}:
        raise ValueError("resource_effect_schema_unsupported")
    if set(destination.get("required", [])) - set(mapping):
        raise ValueError("resource_effect_mapping_required_field_missing")
    for field, value in mapping.items():
        target = destination.get("properties", {}).get(field)
        if not isinstance(target, dict):
            raise ValueError(f"resource_effect_mapping_field_unavailable:{field}")
        _mapping_value(value, target, source, identity)


async def validate_resource_dependencies(repository: Any, package: Any) -> None:
    exports = []
    for export in package.action_exports:
        descriptor = await repository.get_action_descriptor(export.action_descriptor_ref)
        if descriptor.get("resource_effects"):
            exports.append((export, descriptor))
    if not exports:
        return
    packages = await repository.list_capability_packages(
        workspace_id=package.workspace_id or "workspace-default",
        run_id=package.source_run_ref if package.scope == "run_bound" else None,
    )
    packages = [package, *[item for item in packages if item.package_digest != package.package_digest]]

    async def schemas(export: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        contract = IoContract.model_validate(await repository._load_json_content(export.io_contract_ref))
        input_schema = await repository._load_json_content(contract.input_schema_ref) if contract.input_schema_ref else {}
        output_schema = await repository._load_json_content(contract.output_schema_ref) if contract.output_schema_ref else {}
        return input_schema, output_schema

    async def action(ref: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        exact = ResourceRef.model_validate(ref.model_dump(mode="json"))
        if not exact.digest:
            raise ValueError("resource_effect_action_digest_required")
        matches = []
        for candidate in packages:
            for export in candidate.action_exports:
                actual = export.action_descriptor_ref
                if actual.resource_id == exact.resource_id and actual.digest == exact.digest:
                    matches.append(export)
        if not matches:
            raise ValueError("resource_effect_action_unavailable")
        if len({export.io_contract_ref.digest for export in matches}) != 1:
            raise ValueError("resource_effect_action_ambiguous")
        await repository.get_action_descriptor(exact)
        return await schemas(matches[0])

    for export, descriptor in exports:
        _, output_schema = await schemas(export)
        for raw in descriptor["resource_effects"]:
            effect = ResourceEffect.model_validate(raw)
            await repository.get_mcue_definition(package.workspace_id or repository.settings.workspace_id, effect.mcue_ref)
            identity = _at(output_schema, effect.identity_path)
            if _types(identity) != {"string"}:
                raise ValueError("resource_effect_identity_type_invalid")
            status_schema = output_schema
            if effect.readiness:
                readiness = effect.readiness
                input_schema, readiness_output = await action(readiness.action_ref)
                _mapping(readiness.input_mapping, input_schema, output_schema, identity)
                status_schema = deepcopy(_at(readiness_output, readiness.result_path))
                if _types(_at(status_schema, readiness.identity_path)) != {"string"}:
                    raise ValueError("resource_effect_identity_type_invalid")
                for condition in readiness.conditions:
                    condition_schema = _at(status_schema, condition.path)
                    if condition.operator in {"equals", "not_equals"}:
                        if not Draft202012Validator(condition_schema).is_valid(condition.value):
                            raise ValueError("resource_effect_condition_type_mismatch")
                    if condition.operator == "present" and isinstance(condition_schema.get("type"), list):
                        condition_schema["type"] = [item for item in condition_schema["type"] if item != "null"]
            for access in effect.access_requirements:
                if not access.package_ref.digest:
                    raise ValueError("resource_effect_package_digest_required")
                target = next((
                    candidate for candidate in packages
                    if repository._package_ref(candidate) == access.package_ref.resource_id
                    and candidate.package_digest == access.package_ref.digest
                ), None)
                if target is None:
                    raise ValueError("resource_effect_access_package_unavailable")
                kinds = set()
                for access_export in target.action_exports:
                    access_descriptor = await repository.get_action_descriptor(access_export.action_descriptor_ref)
                    kinds.update(access_descriptor.get("capability_kinds", []))
                if access.capability_kind not in kinds:
                    raise ValueError("resource_effect_capability_kind_mismatch")
                def check_sources(values: list[Any]) -> None:
                    for value in values:
                        if isinstance(value, str) and value != "$resource.identity":
                            _at(status_schema, value)
                        elif isinstance(value, list):
                            check_sources(value)

                check_sources(list(access.instance_config_mapping.values()))
                config_schema = await activation_config_schema(repository, target)
                if access.instance_config_mapping and config_schema is None:
                    raise ValueError("activation_config_schema_required")
                if config_schema is not None:
                    _mapping(access.instance_config_mapping, config_schema, status_schema, identity)
            if effect.cleanup:
                cleanup_input, _ = await action(effect.cleanup.action_ref)
                _mapping(effect.cleanup.input_mapping, cleanup_input, output_schema, identity)
