from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.types import CapabilityPackageVersion, ResourceRef
from loom_v2.observer.repository import ObserverRepository
from loom_v2.testing.observer import create_embedded_app
from tests.support.package_fixture import capability_export, register_test_descriptor


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
    descriptor = register_test_descriptor(repo, f"loom://{package_id}/ssh-connect")
    return {
        "package_id": package_id,
        "package_version": "v1",
        "package_type": "interface",
        "execution": {"kind": "none", "version": "1"},
        "capability_exports": [capability_export(descriptor, contract_ref)],
        "body": None,
    }


@pytest.mark.asyncio
async def test_interface_package_can_describe_resource_without_activation() -> None:
    repo = ObserverRepository()
    manifest = await _interface_package(repo)
    with TestClient(create_embedded_app(repo)) as client:
        uploaded = client.post("/api/v1/capability-packages", json=manifest)
        assert uploaded.status_code == 200, uploaded.text
        package = uploaded.json()
        assert package["body"] is None
        assert package["execution"] == {"kind": "none", "version": "1"}
        package_version_ref = f"capability-package://{package['package_id']}/{package['package_version']}"

        package_ref = {
            "resource_id": f"capability-package://{package['package_id']}/{package['package_version']}",
            "version_or_digest": package["package_digest"],
        }
        resource = {
            "resource_ref": {"resource_id": "resource://suanli/vm/i-123"},
            "package_ref": package_ref,
            "state": "running",
        }
        response = client.post("/api/v1/resources", json=resource)
        assert response.status_code == 200, response.text
        assert response.json()["package_ref"]["resource_id"] == package_ref["resource_id"]
        assert response.json()["package_ref"]["version_or_digest"] == package_ref["version_or_digest"]
        assert client.get("/api/v1/resources").json()[0]["resource_ref"]["resource_id"] == "resource://suanli/vm/i-123"
        assert client.get("/api/v1/resources/resource://suanli/vm/i-123").status_code == 200

        activation = client.post(
            f"/api/v1/capability-packages/{package_version_ref}/promote",
            json={"approved_digest": package["package_digest"], "target_slaves": ["slave-a"]},
        )
        assert activation.status_code == 409
        assert activation.json()["detail"] == "capability_package_not_executable"


@pytest.mark.asyncio
async def test_resource_manager_must_be_registered_and_binding_is_immutable() -> None:
    repo = ObserverRepository()
    manifest = await _interface_package(repo, "resource-use")
    with TestClient(create_embedded_app(repo)) as client:
        package = client.post("/api/v1/capability-packages", json=manifest).json()
        package_version_ref = f"capability-package://{package['package_id']}/{package['package_version']}"
        package_ref = {
            "resource_id": f"capability-package://{package['package_id']}/{package['package_version']}",
            "version_or_digest": package["package_digest"],
        }
        child = {
            "resource_ref": {"resource_id": "resource://vm/child"},
            "package_ref": package_ref,
            "managed_by_ref": {"resource_id": "resource://cloud/missing"},
        }
        missing = client.post("/api/v1/resources", json=child)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "resource_reference_not_found"

        manager = {
            "resource_ref": {"resource_id": "resource://cloud/account"},
            "package_ref": package_ref,
            "state": "ready",
        }
        assert client.post("/api/v1/resources", json=manager).status_code == 200
        child["managed_by_ref"] = manager["resource_ref"]
        assert client.post("/api/v1/resources", json=child).status_code == 200
        other_manifest = await _interface_package(repo, "resource-use-other")
        other = client.post("/api/v1/capability-packages", json=other_manifest).json()
        assert client.post(
            f"/api/v1/capability-packages/capability-package://{other['package_id']}/{other['package_version']}/promote",
            json={"approved_digest": other["package_digest"]},
        ).status_code == 200
        other_ref = {
            "resource_id": f"capability-package://{other['package_id']}/{other['package_version']}",
            "version_or_digest": other["package_digest"],
        }
        child["package_ref"] = other_ref
        conflict = client.post("/api/v1/resources", json=child)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "resource_binding_conflict"


def test_interface_package_model_has_no_runtime_body() -> None:
    package = CapabilityPackageVersion(
        package_id="vm-use",
        package_version="v1",
        package_type="interface",
        execution={"kind": "none", "version": "1"},
        capability_exports=[
            {
                "capability_descriptor_ref": ResourceRef(
                    resource_id="loom://vm/ssh",
                    version_or_digest="a" * 64,
                    identity_criterion="descriptor_digest",
                ),
                "io_contract_ref": ResourceRef(
                    resource_id="content://sha256/" + "b" * 64,
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
