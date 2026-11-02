from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.types import CapabilityPackageVersion, ResourceRef
from loom_v2.pool.repository import PoolRepository as ObserverRepository
from loom_v2.testing.observer import create_embedded_app
from tests.support.package_fixture import action_export, register_test_descriptor


async def _interface_package(repo: ObserverRepository, package_id: str = "vm-ssh") -> dict:
    contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": None,
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    descriptor = register_test_descriptor(repo, f"grip://loom.local/workspaces/workspace-default/actions/{package_id}/ssh-connect")
    return {
        "package_id": package_id,
        "package_version": "v1",
        "package_type": "interface",
        "execution": {"kind": "none", "version": "1"},
        "action_exports": [action_export(descriptor, contract_ref)],
        "body": None,
    }


@pytest.mark.asyncio
async def test_resource_registration_is_independent_of_published_interface_packages():
    from tests.support.grip_client import invoke, resources, exchange
    repo = ObserverRepository()
    manifest = await _interface_package(repo)
    with TestClient(create_embedded_app(repo)) as client:
        n = client.app.state.grip.names
        value = {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/suanli/vm/i-123", "mcue_ref": n.mcue("computing/vm"), "state": "running"}
        created = invoke(client, n.root, n.action("register-resource"), value)
        assert invoke(client, n.root, n.action("register-resource"), value) == created
        description = exchange(client, created["resource"])["body"]
        assert "package" not in description["resource"]["metadata"]
        assert description["resource"]["metadata"]["activations"] == []
        assert [o["action"]["uri"] for o in description["offers"]] == [n.action("update-attributes"), n.action("how-to-use")]
        assert resources(client, "computing/vm")[0]["uri"] == created["resource"]
        uri = client.post("/api/v1/capability-packages", json=manifest).json()["resource"]
        assert exchange(client, created["resource"])["body"] == description
        schema = invoke(client, n.schema("register-resource-input"), n.action("read"))
        assert "package" not in schema["properties"]
        assert exchange(client, n.root, action=n.action("register-resource"), input={**value, "package": uri})["error_code"] == "invalid_input"
        result = exchange(client, n.name("slaves", "slave-a"), action=n.action("activate-package"), input={"package": uri, "idempotency_key": "interface"})
        assert result["error_code"] == "invalid_input"


@pytest.mark.asyncio
async def test_resource_how_to_use_explains_attributes_and_is_read_only():
    from tests.support.grip_client import invoke
    repo = ObserverRepository()
    with TestClient(create_embedded_app(repo)) as client:
        n = client.app.state.grip.names
        resource = invoke(client, n.root, n.action("register-resource"), {
            "resource_id": "grip://loom.local/workspaces/workspace-default/resources/vm/help",
            "mcue_ref": n.mcue("computing/vm"),
            "attributes": {"display_name": "demo2zy", "cpu_cores": 4},
            "state": "running",
        })["resource"]
        result = invoke(client, resource, n.action("how-to-use"))
        assert result["resource"] == resource
        assert result["summary"].startswith("computing/vm resource")
        assert "attributes" not in result and "relationships" not in result
        assert {item["name"] for item in result["attribute_help"]} >= {"display_name", "cpu_cores"}
        assert n.action("update-attributes") in {item["action"] for item in result["actions"]}
        assert n.action("how-to-use") not in {item["action"] for item in result["actions"]}
        assert "No callable business action" not in result["usage_markdown"]
        assert not repo.runs


@pytest.mark.asyncio
async def test_package_how_to_use_includes_published_documentation_and_exports():
    from tests.support.grip_client import exchange, invoke
    repo = ObserverRepository()
    manifest = await _interface_package(repo, "documented")
    documentation = await repo.put_content("# Documented package\n\nActivate this package before invoking its export.", media_type="text/markdown")
    manifest["how_to_use_ref"] = documentation.model_dump(mode="json")
    with TestClient(create_embedded_app(repo)) as client:
        n = client.app.state.grip.names
        package = client.post("/api/v1/capability-packages", json=manifest).json()["resource"]
        result = invoke(client, package, n.action("how-to-use"))
        description = exchange(client, package)["body"]
        assert result["documentation"]["content_ref"] == documentation.resource_id
        assert "Activate this package" in result["documentation"]["markdown"]
        assert "Declared actions" in result["usage_markdown"]
        assert result["actions"]
        assert result["actions"][0]["action"] == description["resource"]["metadata"]["actions"][0]["uri"]
        assert result["actions"][0]["available"] is False


@pytest.mark.asyncio
async def test_resource_manager_binding_remains_immutable_without_a_package():
    from tests.support.grip_client import invoke, exchange
    repo = ObserverRepository()
    with TestClient(create_embedded_app(repo)) as client:
        n = client.app.state.grip.names
        child = {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/child", "mcue_ref": n.mcue("computing/vm"), "managed_by": n.name("resources", "manager")}
        assert exchange(client, n.root, action=n.action("register-resource"), input=child)["error_code"] == "not_found"
        manager = invoke(client, n.root, n.action("register-resource"), {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/manager", "mcue_ref": n.mcue("computing/vm")})
        assert manager["resource"] == child["managed_by"]
        other = invoke(client, n.root, n.action("register-resource"), {
            "resource_id": n.name("resources", "other-manager"), "mcue_ref": n.mcue("computing/vm"),
        })["resource"]
        created = invoke(client, n.root, n.action("register-resource"), child)
        assert invoke(client, n.root, n.action("register-resource"), child) == created
        invoke(client, n.root, n.action("register-resource"), {**child, "state": "ready"})
        assert exchange(client, created["resource"])["body"]["resource"]["metadata"]["state"] == "ready"
        assert exchange(client, n.root, action=n.action("register-resource"), input={**child, "managed_by": other})["error_code"] == "invalid_input"


def test_interface_package_model_has_no_runtime_body() -> None:
    package = CapabilityPackageVersion(
        package_id="vm-use",
        package_version="v1",
        package_type="interface",
        execution={"kind": "none", "version": "1"},
        action_exports=[
            {
                "action_descriptor_ref": ResourceRef(
                    resource_id="grip://loom.local/workspaces/workspace-default/actions/vm/ssh",
                    version_or_digest="a" * 64,
                    identity_criterion="descriptor_digest",
                ),
                "io_contract_ref": ResourceRef(
                    resource_id="grip://loom.local/workspaces/workspace-default/content/document/sha256/" + "b" * 64,
                    identity_criterion="content_digest",
                ),
                "effect_class": "ExternalService",
                "replay_safety": "DeclaredByPackage",
            }
        ],
        body=None,
    )
    assert package.is_interface
    assert not package.is_executable
