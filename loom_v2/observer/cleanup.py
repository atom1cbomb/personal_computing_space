"""Durable program steps and explicitly registered cleanup continuations."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
import math

from loom_v2.contracts.types import IoContract, ManagedResource, ResourceRef
from loom_v2.contracts.io_schema import validate


def registrations(record: Any) -> list[dict[str, Any]]:
    return [
        event for event in record.events
        if event.get("phase") == "cleanup_registered" and event.get("execution_id") == record.execution_id
    ]


def continuation(record: Any) -> dict[str, Any] | None:
    return next((
        event for event in reversed(record.events)
        if event.get("phase") == "cleanup_started" and event.get("execution_id") == record.execution_id
    ), None)


def registered_intent(record: Any, intent_id: str) -> dict[str, Any] | None:
    return next((event for event in registrations(record) if event["intent_id"] == intent_id), None)


def assert_cleanup_node(record: Any, node: Any) -> None:
    if record.state == "running":
        if node.intent_id.startswith("cleanup:"):
            raise ValueError("cleanup_not_started")
        return
    event = registered_intent(record, node.intent_id)
    if record.state != "cleaning_up" or event is None:
        raise ValueError("execution_not_running")
    expected = {
        "package_ref": node.package_ref.model_dump(mode="json"),
        "action_descriptor_ref": node.action_descriptor_ref.model_dump(mode="json"),
        "input_refs": [ref.model_dump(mode="json") for ref in node.input_refs],
    }
    if any(expected[key] != event[key] for key in expected) or node.target_resource_ref or node.activation_ref or node.instance_name != "default":
        raise ValueError("cleanup_intent_conflict")


def _mapping_value(value: Any, identity: str) -> Any:
    if value == "$resource.identity":
        return identity
    if isinstance(value, dict) and set(value) == {"constant"}:
        return value["constant"]
    if isinstance(value, list):
        return [_mapping_value(item, identity) for item in value]
    raise ValueError("cleanup_input_mapping_invalid")


async def materialize_json(repository: Any, run_id: str, *, execution_id: str, step_id: str, value: Any) -> ResourceRef:
    record = await repository.get_run(run_id)
    if record.execution_id != execution_id or record.state != "running" or not step_id:
        raise ValueError("execution_not_running")
    ref = await repository.put_content(value, media_type="application/json")
    previous = next((event for event in record.events if event.get("phase") == "program_content_materialized"
                     and event.get("execution_id") == execution_id and event.get("step_id") == step_id), None)
    if previous is not None:
        if previous["content_ref"] != ref.model_dump(mode="json"):
            raise ValueError("program_step_conflict")
        return ref
    record.events.append({
        "phase": "program_content_materialized", "execution_id": execution_id,
        "step_id": step_id, "content_ref": ref.model_dump(mode="json"),
    })
    await repository._persist(record)
    return ref


async def register_program_resource(repository: Any, run_id: str, *, execution_id: str, step_id: str,
                                    mcue_ref: str, identity: str, package_ref: ResourceRef | None = None,
                                    managed_by_ref: ResourceRef | None = None,
                                    attributes: dict[str, Any] | None = None) -> ResourceRef:
    record = await repository.get_run(run_id)
    if record.execution_id != execution_id or record.state != "running" or not step_id or not identity:
        raise ValueError("execution_not_running")
    workspace = record.closure_contract.workspace_id
    definition = await repository.get_mcue_definition(workspace, mcue_ref)
    resource_ref = ResourceRef(resource_id=identity if "://" in identity else repository.names.name("resources", *definition.mcue.split("/"), identity))
    payload = {
        "mcue_ref": mcue_ref, "identity": identity, "attributes": attributes or {},
        "resource_ref": resource_ref.model_dump(mode="json"),
        "package_ref": package_ref.model_dump(mode="json") if package_ref else None,
        "managed_by_ref": managed_by_ref.model_dump(mode="json") if managed_by_ref else None,
    }
    previous = next((event for event in record.events if event.get("phase") == "program_resource_registered"
                     and event.get("execution_id") == execution_id and event.get("step_id") == step_id), None)
    if previous is not None:
        if previous["arguments"] != payload:
            raise ValueError("program_step_conflict")
        return ResourceRef.model_validate(previous["arguments"]["resource_ref"])
    if package_ref is not None:
        package = await repository.get_capability_package(package_ref, run_id=run_id)
        orchestrator = await repository.get_capability_package(record.committed.snapshot.program_systems.package_ref, run_id=run_id)
        allowed = [ResourceRef.model_validate(value) for value in orchestrator.body["allowed_node_package_refs"]]
        if not any(ref.digest == package.package_digest and ref.resource_id == package_ref.resource_id for ref in allowed):
            raise ValueError("node_package_not_allowed")
    await repository.register_resource(workspace, ManagedResource(
        resource_ref=resource_ref, package_ref=package_ref, managed_by_ref=managed_by_ref,
        mcue_ref=mcue_ref, attributes=attributes or {},
    ))
    record.events.append({
        "phase": "program_resource_registered", "execution_id": execution_id,
        "step_id": step_id, "arguments": payload,
    })
    await repository._persist(record)
    return resource_ref


async def ensure_program_activation(repository: Any, run_id: str, *, execution_id: str, step_id: str,
                                    package_ref: ResourceRef, target_slave: str, target_resource_ref: ResourceRef,
                                    instance_name: str, instance_config: dict[str, Any], requires_authentication: bool,
                                    credential_profile: str | None = None) -> ResourceRef:
    record = await repository.get_run(run_id)
    if record.execution_id != execution_id or record.state != "running" or not step_id:
        raise ValueError("execution_not_running")
    arguments = {
        "package_ref": package_ref.model_dump(mode="json"),
        "target_resource_ref": target_resource_ref.model_dump(mode="json"),
        "instance_name": instance_name, "instance_config": instance_config,
        "credential_profile": credential_profile,
        "requires_authentication": requires_authentication,
    }
    previous = next((event for event in record.events if event.get("phase") == "program_activation_ensured"
                     and event.get("execution_id") == execution_id and event.get("step_id") == step_id), None)
    if previous:
        if previous["arguments"] != arguments:
            raise ValueError("program_step_conflict")
        package = await repository.get_capability_package(package_ref, run_id=run_id)
        existing = ResourceRef.model_validate(previous["activation_ref"])
        repository.service_routes.require(package, existing.access_binding["target_slave"], instance_name)
        return existing
    orchestrator = await repository.get_capability_package(record.committed.snapshot.program_systems.package_ref, run_id=run_id)
    if package_ref not in [ResourceRef.model_validate(value) for value in orchestrator.body["allowed_node_package_refs"]]:
        raise ValueError("node_package_not_allowed")
    activation = await repository.set_capability_desired(
        package_ref, target_slave, desired_state="running", idempotency_key=f"{execution_id}:{step_id}",
        run_id=run_id, instance_name=instance_name, target_resource_ref=target_resource_ref, instance_config=instance_config,
        credential_profile=credential_profile,
    )
    activated_package = await repository.get_capability_package(package_ref, run_id=run_id)
    ref = ResourceRef(
        resource_id=repository.names.name("activations", activated_package.package_id, activated_package.package_version, activation.target_slave, activation.instance_name),
        version_or_digest=activation.package_digest,
        access_binding={
            "activation_revision": activation.activation_revision, "package_version_ref": activation.package_version_ref,
            "target_slave": activation.target_slave, "instance_name": activation.instance_name,
            "target_resource_ref": target_resource_ref.model_dump(mode="json"),
        },
    )
    record.events.append({"phase": "program_activation_ensured", "execution_id": execution_id,
                          "step_id": step_id, "arguments": arguments, "activation_ref": ref.model_dump(mode="json")})
    await repository._persist(record)
    return ref


async def register_cleanup(repository: Any, run_id: str, *, execution_id: str, step_id: str,
                           resource_ref: ResourceRef, cleanup_action_ref: ResourceRef,
                           input_mapping: dict[str, Any]) -> dict[str, Any]:
    record = await repository.get_run(run_id)
    if record.execution_id != execution_id or record.state != "running" or not step_id:
        raise ValueError("execution_not_running")
    try:
        timeout = float(record.closure_contract.resource_budget.get("cleanup_timeout_seconds", 60))
        if not math.isfinite(timeout) or not 1 <= timeout <= 300:
            raise ValueError("cleanup_budget_invalid")
    except (TypeError, ValueError) as exc:
        raise ValueError("cleanup_budget_invalid") from exc
    arguments = {
        "resource_ref": resource_ref.model_dump(mode="json"),
        "cleanup_action_ref": cleanup_action_ref.model_dump(mode="json"),
        "input_mapping": input_mapping,
    }
    previous = next((event for event in registrations(record) if event["step_id"] == step_id), None)
    if previous:
        if previous["arguments"] != arguments:
            raise ValueError("program_step_conflict")
        return previous
    if any(event["arguments"] == arguments for event in registrations(record)):
        raise ValueError("cleanup_already_registered")
    resource_event = next((
        event for event in record.events if event.get("phase") == "program_resource_registered"
        and event.get("execution_id") == execution_id
        and event["arguments"]["resource_ref"] == arguments["resource_ref"]
    ), None)
    if resource_event is None:
        raise ValueError("cleanup_resource_not_registered")
    snapshot = record.committed.snapshot
    orchestrator = await repository.get_capability_package(snapshot.program_systems.package_ref, run_id=run_id)
    matches = []
    for value in orchestrator.body["allowed_node_package_refs"]:
        package_ref = ResourceRef.model_validate(value)
        package = await repository.get_capability_package(package_ref, run_id=run_id)
        for export in package.action_exports:
            if export.action_descriptor_ref == cleanup_action_ref:
                matches.append((package_ref, export))
    if len(matches) != 1:
        raise ValueError("cleanup_action_unavailable_or_ambiguous")
    package_ref, export = matches[0]
    if not set(export.permissions) <= set(record.closure_contract.allowed_effects):
        raise ValueError("node_permission_denied")
    payload = {key: _mapping_value(value, resource_event["arguments"]["identity"]) for key, value in input_mapping.items()}
    contract = IoContract.model_validate(await repository._load_json_content(export.io_contract_ref))
    if contract.input_schema_ref:
        schema = await repository._load_json_content(contract.input_schema_ref)
        if validate(schema, payload):
            raise ValueError("cleanup_input_schema_mismatch")
    if len(record.dynamic_nodes) + len(registrations(record)) + 1 > int(orchestrator.body["max_nodes"]):
        raise ValueError("orchestration_node_limit_exceeded")
    input_ref = await repository.put_content(payload, media_type="application/json")
    event = {
        "phase": "cleanup_registered", "execution_id": execution_id, "step_id": step_id,
        "intent_id": f"cleanup:{step_id}", "arguments": arguments,
        "package_ref": package_ref.model_dump(mode="json"),
        "action_descriptor_ref": cleanup_action_ref.model_dump(mode="json"),
        "input_refs": [input_ref.model_dump(mode="json")],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    record.events.append(event)
    await repository._persist(record)
    return event


async def finish_cleanup(repository: Any, run_id: str, *, execution_id: str,
                         errors: list[dict[str, Any]]) -> Any:
    record = await repository.get_run(run_id)
    if execution_id != record.execution_id:
        raise ValueError("stale_execution_id")
    if record.state != "cleaning_up":
        raise ValueError("cleanup_not_running")
    started = continuation(record)
    if started is None:
        raise ValueError("cleanup_not_started")
    errors = deepcopy(errors)
    for event in registrations(record):
        node = next((node for node in record.dynamic_nodes if node.intent_id == event["intent_id"]), None)
        if node is None or node.state != "completed":
            errors.append({"step_id": event["step_id"], "resource_ref": event["arguments"]["resource_ref"],
                           "code": "cleanup_unconfirmed"})
    record.state = started["terminal_state"]
    record.outcome = deepcopy(started["outcome"]) or {}
    record.outcome["cleanup"] = {"state": "failed" if errors else "completed", "errors": errors}
    if errors and record.state == "completed":
        record.state = "failed"
        record.outcome.update(disposition="failed", terminal_error={"code": "cleanup_failed"})
    record.events.append({"phase": "cleanup_finished", "execution_id": execution_id, "errors": errors})
    await repository._persist(record)
    await repository._settle_execution_request(run_id, execution_id=execution_id, outcome=record.outcome)
    return record
