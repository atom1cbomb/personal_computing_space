"""All resource identities, including package artifacts, are named GRIP URIs."""
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from loom_orchestration import LoomClient, Names, ServicePackage
from loom_orchestration.contracts import ResourceRef as SdkRef
from loom_orchestration.naming import content_uri
from loom_v2.content_store import ContentStore
from loom_v2.contracts.types import ResourceRef
from loom_v2.observer.app import create_app
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from tests.support.package_fixture import process_package_value


@pytest.mark.parametrize("model", [ResourceRef, SdkRef])
@pytest.mark.parametrize("old", ["content://sha256/" + "a" * 64, "loom://suanlicloud/instances-list", "capability-package://test/v1", "slave-a"])
def test_legacy_resource_identities_are_rejected(model, old):
    with pytest.raises(ValidationError):
        model(resource_id=old)


@pytest.mark.parametrize("name", ["a" * 64, "123456", "../schema", "schema/../input", "/schema", "schema/", "content://schema"])
def test_content_names_require_readable_canonical_segments(name):
    with pytest.raises(ValueError):
        content_uri("b" * 64, name)


async def test_names_survive_restart_and_cannot_be_forged():
    settings = Settings(grip_authority="compute.example", workspace_id="research")
    first = ContentStore.from_settings(settings)
    ref = await first.put(b'{"type":"object"}', media_type="application/schema+json", name="cloud/schemas/instances-output")
    assert "/cloud/schemas/instances-output/sha256/" in ref.resource_id
    second = ContentStore.from_settings(settings)
    assert await second.get(ref) == b'{"type":"object"}'
    forged = ref.model_copy(update={"resource_id": ref.resource_id.replace("instances-output", "secrets")})
    assert not await second.exists(forged)
    with pytest.raises(FileNotFoundError):
        await second.get(forged)
    other = ContentStore.from_settings(settings.model_copy(update={"workspace_id": "other"}))
    assert not await other.exists(ref)
    # Different uses are explicitly named, with deduplicated bytes.
    alias = await first.put(b'{"type":"object"}', media_type="application/schema+json", name="cloud/schemas/instances-input")
    assert alias.resource_id != ref.resource_id and alias.digest == ref.digest
    assert await second.get(alias) == await second.get(ref)


async def test_service_publication_rewrites_every_artifact_to_deployment_namespace():
    settings = Settings(grip_authority="compute.example", workspace_id="research", grip_token="test-token", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    app = create_app(repo, settings=settings)
    root = Path(__file__).resolve().parents[2] / "capability_services"
    options = dict(token="test-token", allow_insecure_grip=True, grip_authority=settings.grip_authority, workspace_id=settings.workspace_id)
    async with LoomClient("http://observer", transport=httpx.ASGITransport(app=app), **options) as client:
        ssh = await ServicePackage(root / "suanli_vm_ssh_http").publish(client, image_ref="example/ssh@sha256:" + "a" * 64)
        package_uri = await ServicePackage(root / "suanlicloud_http").publish(client, image_ref="example/cloud@sha256:" + "b" * 64, dependencies={Names().name("packages", "suanli-vm-ssh-http", "v2"): ssh})
        assert package_uri == client.names.name("packages", "suanlicloud-http", "v1")
        package = await client.invoke(package_uri, client.names.action("read"), {})
        assert package["resource"] == package_uri
        descriptor = next(e for e in package["action_exports"] if "/descriptors/instances-run/" in e["action_descriptor_ref"]["resource_id"])
        value = await client.read_json(SdkRef.model_validate(descriptor["action_descriptor_ref"]))
        assert value["resource_id"] == client.names.name("actions", "suanlicloud", "instances-run")
        assert value["resource_effects"][0]["access_requirements"][0]["package_ref"]["resource_id"] == ssh
        for export in package["action_exports"]:
            contract_ref = SdkRef.model_validate(export["io_contract_ref"])
            contract = await client.read_json(contract_ref)
            for field in ("input_schema_ref", "output_schema_ref"):
                ref = SdkRef.model_validate(contract[field])
                assert ref.resource_id.startswith(client.names.root + "/content/suanlicloud-http/schemas/")
                assert (await client.read_json(ref))["type"] == "object"
                assert (await client.describe(ref.resource_id))["resource"]["uri"] == ref.resource_id
        exported = (await client.describe(package_uri))["resource"]["metadata"]["actions"]
        assert any("/suanlicloud-http/v1/instances-list/sha256/" in e["uri"] for e in exported)
        assert all("%3A" not in e["uri"] for e in exported)
        ref = await client.upload_content([1, 2, 3], name="reports/sample-values")
        assert await client.read_json(ref) == [1, 2, 3]
    # Reading a persisted content resource does not depend on a process-local catalog.
    async with LoomClient("http://observer", transport=httpx.ASGITransport(app=create_app(settings=settings)), **options) as restarted:
        assert await restarted.read_json(ref) == [1, 2, 3]


@pytest.mark.parametrize("authority,workspace", [
    ("loom.local", "workspace-default"),
    ("compute.example", "research"),
])
async def test_publication_returns_authoritative_name_for_discovery_and_reads(authority, workspace):
    settings = Settings(grip_authority=authority, workspace_id=workspace, grip_token="test-token", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    app = create_app(repo, settings=settings)
    names = Names(authority, workspace)
    manifest = process_package_value(
        repo, package_id="published-package", action_ref=names.name("actions", "echo"),
        program_content_ref=await repo.put_content("print(1)", media_type="text/x-python"),
        io_contract_ref=await repo.put_content({"schema_version": "io.v1"}, media_type="application/vnd.loom.io-contract+json"),
    )
    async with httpx.AsyncClient(base_url="http://observer", transport=httpx.ASGITransport(app=app)) as http:
        async with LoomClient("http://observer", client=http, token="test-token", allow_insecure_grip=True,
                              grip_authority=authority, workspace_id=workspace) as client:
            package = await client.register_package(manifest)
            target = package["resource"]
            assert target == names.name("packages", "published-package", "v1")
            assert (await client.register_package(manifest))["resource"] == target
            assert (await client.describe(target))["resource"]["uri"] == target
            assert (await client.invoke(target, names.action("read"), {}))["resource"] == target
            assert (await client.resources(kind="capability-package"))[0]["uri"] == target
            assert (await client.packages())[0]["resource"] == target
            detail = await http.get(f"/api/v1/capability-packages/{target}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["resource"] == target
            stored = await repo.get_capability_package(target)
            assert "resource" not in stored.model_dump()
            assert stored.package_digest == package["package_digest"]


async def test_service_publisher_uses_returned_name_without_reconstructing_it(monkeypatch):
    settings = Settings(grip_token="test-token", public_base_url="http://observer")
    root = Path(__file__).resolve().parents[2] / "capability_services"
    assigned = "grip://compute.example/workspaces/research/packages/assigned-ssh/release-one"
    async with LoomClient("http://observer", token="test-token", allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=create_app(settings=settings))) as client:
        async def register_package(manifest):
            assert manifest["package_id"] == "vm-ssh-http"
            return {"resource": assigned, "package_id": "vm-ssh-http", "package_version": "v2"}

        monkeypatch.setattr(client, "register_package", register_package)
        published = await ServicePackage(root / "vm_ssh_http").publish(client, image_ref="example/ssh@sha256:" + "a" * 64)
        assert published == assigned
