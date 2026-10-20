from __future__ import annotations

from typing import Any
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.types import IoContract, ResourceRef
from loom_v2.digest import digest_json
from loom_v2.observer.repository import ObserverRepository
from loom_v2.testing.observer import create_embedded_app


@pytest.mark.parametrize("config,code", [
    ({"host": "127.0.0.1", "port": "22", "user": "root"}, "activation_config_invalid"),
    ({"host": "127.0.0.1", "port": 22}, "activation_config_invalid"),
    ({"host": "127.0.0.1", "port": 22, "user": "root", "password": "not-a-secret"}, "activation_config_field_unknown"),
])
async def test_activation_config_rejected_before_desire(config, code):
    repo = ObserverRepository()
    await _register_packages(repo)
    package = next(item for item in await repo.list_capability_packages() if item.package_id == "suanli-vm-ssh-http")
    with pytest.raises(ValueError, match=code):
        await repo.set_capability_desired(repo._package_ref(package), "slave-a", desired_state="running", idempotency_key="config", instance_config=config)
    assert await repo.list_package_activations(package) == []


async def test_activation_config_reuse_preserves_validated_config():
    repo = ObserverRepository()
    await _register_packages(repo)
    package = next(item for item in await repo.list_capability_packages() if item.package_id == "suanli-vm-ssh-http")
    config = {"host": "127.0.0.1", "port": 22, "user": "root"}
    first = await repo.set_capability_desired(repo._package_ref(package), "slave-a", desired_state="running", idempotency_key="first", instance_config=config)
    reused = await repo.set_capability_desired(repo._package_ref(package), "slave-a", desired_state="running", idempotency_key="reuse")
    assert reused.activation_revision == first.activation_revision
    assert reused.instance_config == config


def _descriptor(repo: ObserverRepository, payload: dict[str, Any]) -> ResourceRef:
    digest = repo.register_action_descriptor(payload["resource_id"], payload)
    return ResourceRef(
        resource_id=payload["resource_id"],
        version_or_digest=digest,
        identity_criterion="descriptor_digest",
    )


async def _contract(repo: ObserverRepository) -> ResourceRef:
    output_ref = await repo.put_content(
        {
            "type": "object",
            "properties": {
                "result": {
                    "type": "object",
                    "properties": {
                        "Instances": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "InstanceId": {"type": "string"},
                                    "State": {"type": "string"},
                                    "Ip": {"type": ["string", "null"]},
                                },
                                "required": ["InstanceId"],
                            },
                        }
                    },
                }
            },
        },
        media_type="application/schema+json",
    )
    return await repo.put_content(
        IoContract(output_schema_ref=output_ref).model_dump(mode="json"),
        media_type="application/vnd.loom.io-contract+json",
    )


async def _register_packages(repo: ObserverRepository, *, mutate=None, access_workspace="workspace-default") -> tuple[str, str]:
    async def contract(input_schema, output_schema):
        input_ref = await repo.put_content(input_schema, media_type="application/schema+json")
        output_ref = await repo.put_content(output_schema, media_type="application/schema+json")
        return await repo.put_content(
            IoContract(input_schema_ref=input_ref, output_schema_ref=output_ref).model_dump(mode="json"),
            media_type="application/vnd.loom.io-contract+json",
        )

    def export(descriptor, io_contract, path):
        return {
            "action_descriptor_ref": descriptor.model_dump(mode="json"),
            "io_contract_ref": io_contract.model_dump(mode="json"),
            "effect_class": "ExternalService",
            "permissions": [],
            "replay_safety": "NotReplaySafe",
            "runtime_binding": {"path": path},
        }

    def manifest(package_id, exports, version="v1"):
        return {
            "package_type": "service", "package_id": package_id, "package_version": version,
            "execution": {"kind": "container:http", "version": "1"},
            "body": {
                "image_ref": "registry.example/service@sha256:" + "a" * 64,
                "container_port": 8080, "health_path": "/healthz",
            },
            "action_exports": exports,
        }

    empty = {"type": "object", "properties": {}}
    identity_input = {
        "type": "object", "required": ["InstanceIds"],
        "properties": {"InstanceIds": {"type": "array", "items": {"type": "string"}}},
    }
    item_schema = {
        "type": "object", "required": ["InstanceId", "State"],
        "properties": {
            "InstanceId": {"type": "string"}, "State": {"type": "string"},
            "Ip": {"type": ["string", "null"]},
        },
    }
    status_output = {
        "type": "object", "properties": {
            "result": {"type": "object", "properties": {
                "InstanceStatuses": {"type": "array", "items": item_schema},
            }},
        },
    }
    status = _descriptor(repo, {
        "schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-status",
        "name": "instances_status",
    })
    terminate = _descriptor(repo, {
        "schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-terminate",
        "name": "instances_terminate",
    })
    ssh = _descriptor(repo, {
        "schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/suanli-vm/exec",
        "name": "exec", "capability_kinds": ["compute.vm.remote_shell"],
        "instance_config_schema_ref": (await repo.put_content({
            "type": "object", "required": ["host", "port", "user"],
            "properties": {
                "host": {"type": "string"}, "port": {"type": "integer"},
                "user": {"type": "string"},
            },
        }, media_type="application/schema+json")).model_dump(mode="json"),
    })
    ssh_package = await repo.register_workspace_package(access_workspace, manifest(
        "suanli-vm-ssh-http", [export(ssh, await contract(empty, empty), "/v1/exec")], "v2",
    ))
    effect = {
        "effect": "create", "resource_type": "compute.vm",
        "identity_path": "result.Instances[].InstanceId",
        "readiness": {
            "action_ref": status.model_dump(mode="json"),
            "result_path": "result.InstanceStatuses[]", "identity_path": "InstanceId",
            "input_mapping": {"InstanceIds": ["$resource.identity"]},
            "conditions": [
                {"path": "State", "operator": "equals", "value": "RUNNING"},
                {"path": "Ip", "operator": "present"},
            ],
            "polling": {"max_attempts": 60, "interval_seconds": 5},
        },
        "access_requirements": [{
            "capability_kind": "compute.vm.remote_shell",
            "package_ref": {
                "resource_id": repo._package_ref(ssh_package),
                "version_or_digest": ssh_package.package_digest,
            },
            "instance_config_mapping": {
                "host": "Ip", "port": {"constant": 22}, "user": {"constant": "root"},
            },
            "requires_authentication": True, "ready_state": "authenticated",
        }],
        "cleanup": {
            "action_ref": terminate.model_dump(mode="json"),
            "input_mapping": {"InstanceIds": ["$resource.identity"]},
        },
    }
    if mutate is not None:
        mutate(effect)
    run = _descriptor(repo, {
        "schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-run",
        "name": "instances_run", "resource_effects": [effect],
    })
    await repo.register_workspace_package("workspace-default", manifest("suanlicloud-http", [
        export(run, await _contract(repo), "/v1/instances/run"),
        export(status, await contract(identity_input, status_output), "/v1/instances/status"),
        export(terminate, await contract(identity_input, empty), "/v1/instances/terminate"),
    ]))
    return run.resource_id, status.resource_id


@pytest.mark.asyncio
@pytest.mark.parametrize("change,error", [
    (lambda effect: effect["readiness"]["input_mapping"].clear(), "required_field_missing"),
    (lambda effect: effect["readiness"]["input_mapping"].update(InstanceIds="$resource.identity"), "mapping_type_mismatch"),
    (lambda effect: effect["readiness"].update(identity_path="Unknown"), "schema_path_unavailable"),
    (lambda effect: effect["readiness"]["conditions"][0].update(value=1), "condition_type_mismatch"),
    (lambda effect: effect["access_requirements"][0].update(capability_kind="unknown"), "capability_kind_mismatch"),
    (lambda effect: effect["access_requirements"][0]["package_ref"].update(version_or_digest="b" * 64), "access_package_unavailable"),
    (lambda effect: effect["cleanup"]["action_ref"].update(version_or_digest=None), "action_digest_required"),
    (lambda effect: effect["cleanup"]["action_ref"].update(version_or_digest="b" * 64), "action_unavailable"),
])
async def test_resource_metadata_rejects_invalid_dependencies(change, error):
    repo = ObserverRepository()
    with pytest.raises(ValueError, match=error):
        await _register_packages(repo, mutate=change)
    packages = await repo.list_capability_packages(workspace_id="workspace-default")
    assert not any(package.package_id == "suanlicloud-http" for package in packages)


@pytest.mark.asyncio
async def test_resource_metadata_cannot_reference_access_package_in_another_workspace():
    with pytest.raises(ValueError, match="resource_effect_access_package_unavailable"):
        await _register_packages(ObserverRepository(), access_workspace="workspace-other")


@pytest.mark.asyncio
async def test_resource_metadata_revalidation_rejects_abandoned_dependency():
    repo = ObserverRepository()
    await _register_packages(repo)
    packages = await repo.list_capability_packages(workspace_id="workspace-default")
    access = next(package for package in packages if package.package_id == "suanli-vm-ssh-http")
    cloud = next(package for package in packages if package.package_id == "suanlicloud-http")
    access.publication_state = "abandoned"
    with pytest.raises(ValueError, match="resource_effect_access_package_unavailable"):
        await repo._validate_package_exports(cloud)

@pytest.mark.asyncio
async def test_public_observer_projection_exposes_resource_effects_to_external_agent() -> None:
    repo = ObserverRepository()
    run_ref, status_ref = await _register_packages(repo)
    with TestClient(create_embedded_app(repo)) as client:
        packages = client.get("/api/v1/capability-packages").json()
        cloud = next(item for item in packages if item["package_id"] == "suanlicloud-http")
        run_export = cloud["action_exports"][0]
        descriptor_entry = next(
            item
            for item in cloud["action_descriptors"]
            if item["resource_ref"]["resource_id"] == run_ref
        )
        descriptor = descriptor_entry["descriptor"]
        effect = descriptor["resource_effects"][0]

        assert effect["identity_path"] == "result.Instances[].InstanceId"
        assert effect["readiness"]["action_ref"]["resource_id"] == status_ref
        assert effect["access_requirements"][0]["package_ref"]["resource_id"] == (
            "grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v2"
        )
        assert effect["access_requirements"][0]["requires_authentication"] is True

        ref = run_export["action_descriptor_ref"]
        resolved = client.get(
            "/api/v1/action-descriptors",
            params={"resource_id": ref["resource_id"], "version_or_digest": ref["version_or_digest"]},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["descriptor"] == descriptor


def test_resource_effect_metadata_rejects_unbounded_or_script_paths() -> None:
    from loom_v2.contracts.resource_effects import ResourceEffect

    with pytest.raises(ValueError, match="resource_identity_path_invalid"):
        ResourceEffect.model_validate(
            {"effect": "create", "resource_type": "compute.vm", "identity_path": "result[?(@.id)]"}
        )


@pytest.mark.asyncio
async def test_public_descriptor_endpoint_resolves_content_descriptor_refs() -> None:
    repo = ObserverRepository()
    descriptor = {
        "schema_version": "action.v1",
        "resource_id": "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-run",
        "name": "instances_run",
    }
    descriptor_ref = await repo.put_content(
        descriptor,
        media_type="application/vnd.loom.action-descriptor+json",
    )
    descriptor_digest = digest_json(
        descriptor,
        domain="loom/action-descriptor/v1",
    )
    ref = ResourceRef(
        resource_id=descriptor_ref.resource_id,
        version_or_digest=descriptor_digest,
        identity_criterion="descriptor_digest",
    )

    with TestClient(create_embedded_app(repo)) as client:
        response = client.get(
            "/api/v1/action-descriptors",
            params={"resource_id": ref.resource_id, "version_or_digest": ref.version_or_digest},
        )

    assert response.status_code == 200, response.text
    assert response.json()["descriptor"] == descriptor


def _public_metadata_relationships(client: TestClient, skill_root: Path) -> list[str]:
    """Inspect declared relationships; this does not execute a VM workflow."""
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    assert "resource_effects" in skill
    packages = client.get("/api/v1/capability-packages").json()
    cloud = next(item for item in packages if item["package_id"] == "suanlicloud-http")
    run_descriptor = next(
        item["descriptor"]
        for item in cloud["action_descriptors"]
        if item.get("descriptor", {}).get("resource_id") == "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-run"
    )
    effect = run_descriptor["resource_effects"][0]
    access = effect["access_requirements"][0]
    return [
        "call " + run_descriptor["resource_id"],
        "register resource as " + effect["resource_type"],
        "poll " + effect["readiness"]["action_ref"]["resource_id"],
        "activate " + access["package_ref"]["resource_id"],
        "wait for " + access["ready_state"],
        "execute grip://loom.local/workspaces/workspace-default/actions/suanli-vm/exec",
        "cleanup " + effect["cleanup"]["action_ref"]["resource_id"],
    ]


@pytest.mark.asyncio
async def test_public_metadata_describes_vm_workflow_relationships() -> None:
    repo = ObserverRepository()
    await _register_packages(repo)
    with TestClient(create_embedded_app(repo)) as client:
        plan = _public_metadata_relationships(
            client,
            Path(__file__).parents[2] / "skills/personal-computing-space",
        )
    assert plan == [
        "call grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-run",
        "register resource as compute.vm",
        "poll grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-status",
        "activate grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v2",
        "wait for authenticated",
        "execute grip://loom.local/workspaces/workspace-default/actions/suanli-vm/exec",
        "cleanup grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-terminate",
    ]
