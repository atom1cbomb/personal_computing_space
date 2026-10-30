from copy import deepcopy

import httpx
import pytest

from loom_orchestration import GripError, LoomClient
from loom_v2.observer.app import create_app
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.contracts.types import ResourceRef
from loom_v2.digest import digest_json


@pytest.fixture
async def mcue_client():
    settings = Settings(grip_token="mcue-test", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    async with LoomClient(
        "http://observer", token=settings.grip_token, allow_insecure_grip=True,
        transport=httpx.ASGITransport(app=create_app(repo, settings=settings)),
    ) as client:
        yield client, repo


async def register(client, *, definition=None, attributes=None, name="development"):
    target = client.names.name("resources", "vm", name)
    result = await client.invoke(client.names.root, client.names.action("register-resource"), {
        "resource_id": target, "mcue_ref": definition or client.names.mcue("computing/vm"),
        "attributes": attributes or {},
    })
    assert result == {"resource": target, "attributes_revision": 1}
    return target


async def update(client, target, patch, revision=1, **kwargs):
    return await client.invoke(target, client.names.action("update-attributes"), {
        "expected_attributes_revision": revision, "patch": patch, **kwargs,
    })


async def test_all_resource_descriptions_use_resolvable_mcue(mcue_client):
    client, repo = mcue_client
    target = await register(client)
    catalog = await client.resources()
    assert catalog
    for resource in catalog:
        assert "kind" not in resource
        definition = await client.invoke(resource["metadata"]["mcue_ref"], client.names.action("read"), {})
        assert definition["mcue"] == resource["mcue"]
    assert [r["uri"] for r in await client.resources(mcue="computing/vm")] == [target]
    assert len(await client.resources(mcue="grip/mcue-type")) == 17
    content = await client.upload_content({"answer": 42}, media_type="application/json")
    description = await client.describe(content.resource_id)
    assert description["resource"]["mcue"] == "data/content"
    assert description["resource"]["metadata"]["mcue_ref"] == client.names.mcue("data/content")
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(client.names.root, client.names.action("list-resources"), {"kind": "resource"})
    with pytest.raises(TypeError):
        await client.resources(kind="resource")
    assert not repo.runs


async def test_attributes_patch_validation_revision_and_stable_identity(mcue_client):
    client, repo = mcue_client
    target = await register(client, attributes={"display_name": "Dev", "labels": {"project": "loom", "owner": "alice"}})
    before = await client.describe(target)
    result = await update(client, target, {
        "display_name": "Build", "labels": {"environment": "test", "owner": None},
        "cpu_cores": 4, "disks": [{"name": "system", "size_bytes": 1024}],
    })
    assert result == {"resource": target, "attributes_revision": 2}
    current = await client.describe(target)
    metadata = current["resource"]["metadata"]
    assert metadata["attributes"] == {
        "display_name": "Build", "labels": {"project": "loom", "environment": "test"},
        "cpu_cores": 4, "disks": [{"name": "system", "size_bytes": 1024}],
    }
    assert metadata["package"] is None
    assert current["resource"]["uri"] == target
    assert current["revision"] != before["revision"]
    assert await update(client, target, {}, revision=2) == result
    for patch in ({"cpu_cores": 0}, {"labels": {"owner": 3}}, {"undeclared": "value"}, {"state": "running"}):
        with pytest.raises(GripError, match="mcue_attributes_invalid"):
            await update(client, target, patch, revision=2)
        assert (await client.describe(target))["resource"]["metadata"] == metadata
    with pytest.raises(GripError, match="attributes_revision_conflict"):
        await update(client, target, {"description": "stale"}, revision=1)
    await update(client, target, {"disks": [], "display_name": None}, revision=2)
    attrs = (await client.describe(target))["resource"]["metadata"]["attributes"]
    assert attrs["disks"] == [] and "display_name" not in attrs
    assert not repo.runs


async def test_reregistration_updates_state_without_replacing_attributes(mcue_client):
    client, _ = mcue_client
    target = await register(client, attributes={"display_name": "Keep"})
    data = {"resource_id": target, "mcue_ref": client.names.mcue("computing/vm"), "state": "running"}
    await client.invoke(client.names.root, client.names.action("register-resource"), data)
    metadata = (await client.describe(target))["resource"]["metadata"]
    assert metadata["attributes"] == {"display_name": "Keep"}
    assert metadata["state"] == "running" and metadata["attributes_revision"] == 1
    with pytest.raises(GripError, match="resource_attributes_require_update"):
        await client.invoke(client.names.root, client.names.action("register-resource"), {**data, "attributes": {}})
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(client.names.root, client.names.action("register-resource"), {**data, "resource_id": client.names.root})


async def test_schema_decides_public_connection_fields_and_custom_types(mcue_client):
    client, _ = mcue_client
    payload = {
        "mcue": "network/endpoint", "version": "v1", "description": "Public endpoint",
        "attributes_schema": {
            "type": "object", "additionalProperties": False,
            "$defs": {"hostname": {"type": "string", "minLength": 1}},
            "properties": {
                "host": {"$ref": "#/$defs/hostname"}, "user": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
        },
    }
    published = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), payload)
    assert await client.invoke(client.names.root, client.names.action("publish-mcue-type"), payload) == published
    with pytest.raises(GripError, match="mcue_version_conflict"):
        await client.invoke(client.names.root, client.names.action("publish-mcue-type"), {**payload, "description": "changed"})
    target = await register(client, definition=published["resource"], name="endpoint",
                            attributes={"host": "public.example", "user": "demo", "port": 22})
    assert (await client.describe(target))["resource"]["mcue"] == "network/endpoint"
    await update(client, target, {"port": 2222})
    vm = await register(client)
    with pytest.raises(GripError, match="mcue_attributes_invalid"):
        await update(client, vm, {"host": "public.example"})
    assert (await client.describe(vm))["resource"]["metadata"]["attributes"] == {}
    with pytest.raises(GripError, match="resource_mcue_conflict"):
        await update(client, vm, {}, mcue_ref=published["resource"])


async def test_type_version_change_is_explicit_atomic_and_validated(mcue_client):
    client, _ = mcue_client
    original = await client.invoke(client.names.mcue("computing/vm"), client.names.action("read"), {})
    new = deepcopy(original)
    new["version"] = "v2"
    new["attributes_schema"]["required"] = ["display_name"]
    published = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), new)
    target = await register(client)
    with pytest.raises(GripError, match="mcue_attributes_invalid"):
        await update(client, target, {}, mcue_ref=published["resource"])
    before = (await client.describe(target))["resource"]["metadata"]
    assert before["mcue_ref"] == client.names.mcue("computing/vm") and before["attributes_revision"] == 1
    await update(client, target, {"display_name": "Version two"}, mcue_ref=published["resource"])
    current = (await client.describe(target))["resource"]["metadata"]
    assert current["mcue_ref"] == published["resource"] and current["attributes_revision"] == 2
    assert len(await client.resources(mcue="computing/vm")) == 1
    assert await client.invoke(client.names.mcue("computing/vm"), client.names.action("read"), {}) == original


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"bad": {"type": "not-a-type"}}},
    {"type": "object", "properties": {"bad": {"$ref": "https://example.com/schema"}}},
    {"type": "object", "properties": {"bad": {"$ref": "#/$defs/missing"}}},
    {"type": "object", "$id": "https://example.com/schema"},
    {"type": "array"},
])
async def test_invalid_or_external_schemas_are_not_published(mcue_client, schema):
    client, _ = mcue_client
    with pytest.raises(GripError):
        await client.invoke(client.names.root, client.names.action("publish-mcue-type"), {
            "mcue": "test/invalid", "version": "v1", "attributes_schema": schema,
        })
    assert not any(r["metadata"]["mcue"] == "test/invalid" for r in await client.resources(mcue="grip/mcue-type"))


async def test_historical_descriptor_is_readable_but_not_accepted_for_new_authoring(mcue_client):
    client, repo = mcue_client
    historical = {
        "schema_version": "action.v1", "resource_id": client.names.name("actions", "historical", "create"),
        "resource_effects": [{"effect": "create", "resource_type": "compute.vm", "identity_path": "id"}],
    }
    # This represents already stored immutable bytes from an earlier release.
    content = await repo.put_content(historical, media_type="application/json", name="historical/create")
    ref = ResourceRef(resource_id=content.resource_id,
                      version_or_digest=digest_json(historical, domain="loom/action-descriptor/v1"),
                      identity_criterion="descriptor_digest")
    assert await repo.read_action_descriptor(ref) == historical
    assert await client.descriptor(ref) == historical
    with pytest.raises(ValueError, match="resource_effects_invalid"):
        await repo.get_action_descriptor(ref)
    tampered = ref.model_copy(update={"version_or_digest": "a" * 64})
    with pytest.raises(ValueError, match="action_descriptor_digest_mismatch"):
        await repo.read_action_descriptor(tampered)

    # Restore a historical package and stopped activation as persisted records,
    # without admitting it through today's publication contract.
    from loom_v2.contracts.types import CapabilityPackageVersion, CapabilityPackageActivation, IoContract
    from tests.support.package_fixture import action_export

    contract = await repo.put_content(IoContract().model_dump(mode="json"), media_type="application/vnd.loom.io-contract+json")
    package = CapabilityPackageVersion.model_validate({
        "package_id": "historical", "package_version": "v1", "workspace_id": repo.settings.workspace_id,
        "scope": "workspace_reusable", "publication_state": "published", "package_type": "service",
        "execution": {"kind": "container:http", "version": "1"},
        "body": {"image_ref": "example/historical@sha256:" + "a" * 64, "container_port": 8080, "health_path": "/healthz"},
        "action_exports": [action_export(ref, contract, runtime_binding={"path": "/create"})],
    })
    activation = CapabilityPackageActivation(
        package_version_ref=package.version_ref, package_digest=package.package_digest,
        target_slave="slave-a", instance_name="default", desired_state="stopped", activation_state="stopped",
        activation_revision=1, activation_closure_version_ref="historical", compute_binding_ref="historical",
        last_idempotency_key="historical",
    )
    await repo._persist_workspace_package(package, activations=[activation])
    entries = await client.resources()
    targets = [row["uri"] for row in entries if row["mcue"] in {"capability/package", "capability/activation"}]
    assert len(targets) == 2
    for target in targets:
        assert (await client.describe(target))["resource"]["metadata"]["dependencies"] is None
        assert (await client.invoke(target, client.names.action("how-to-use"), {}))["dependency_fields"] == []
    with pytest.raises(ValueError, match="resource_effects_invalid"):
        await repo._validate_package_exports(package, publication=True)


async def test_json_boolean_and_number_are_distinct_for_revision_and_publication(mcue_client):
    client, _ = mcue_client
    body = {"mcue": "test/json", "version": "v1", "attributes_schema": {"type": "object", "properties": {"value": {"const": 1}}}}
    published = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), body)
    changed = deepcopy(body)
    changed["attributes_schema"]["properties"]["value"]["const"] = True
    with pytest.raises(GripError, match="mcue_version_conflict"):
        await client.invoke(client.names.root, client.names.action("publish-mcue-type"), changed)
    body["version"] = "v2"
    body["attributes_schema"] = {"type": "object"}
    published = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), body)
    target = await register(client, definition=published["resource"], attributes={"value": 1})
    assert (await update(client, target, {"value": True}))["attributes_revision"] == 2
    assert (await client.describe(target))["resource"]["metadata"]["attributes"]["value"] is True


async def test_schema_declared_dependencies_discovery_and_validation(mcue_client):
    from loom_orchestration.workflows import inventory

    client, repo = mcue_client
    host = await register(client, name="host")
    reference = {"type": "string", "description": "Backing store for this shard.", "x-loom-dependency": True}
    definition = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), {
        "mcue": "storage/cluster", "version": "v1", "attributes_schema": {
            "type": "object", "required": ["replicas", "shards"], "properties": {
                "replicas": {"type": "array", "minItems": 1, "items": {"type": "string"},
                             "description": "Replication peers.", "x-loom-dependency": True},
                "shards": {"type": "array", "items": {
                    "type": "object", "required": ["store"], "properties": {"store": reference}}},
            },
        },
    })
    attributes = {"replicas": [host], "shards": [{"store": host}]}
    target = await register(client, definition=definition["resource"], attributes=attributes, name="cluster")
    description = await client.describe(target)
    metadata = description["resource"]["metadata"]
    assert metadata["dependencies"] == [
        {"path": "replicas", "resource": host, "description": "Replication peers."},
        {"path": "shards[].store", "resource": host, "description": reference["description"]},
    ]
    assert (await client.resources(mcue="storage/cluster"))[0] == description["resource"]
    row = next(row for row in (await inventory(client))["resources"] if row["uri"] == target)
    assert row["dependencies"] == metadata["dependencies"] and row["attributes"] == attributes
    help_text = await client.invoke(target, client.names.action("how-to-use"), {})
    assert host not in str(help_text)
    assert "attributes" not in help_text and "relationships" not in help_text
    assert help_text["dependency_fields"][0] == {
        "path": "attributes.replicas", "description": "Replication peers.", "required": True, "many": True}
    for patch, error in [({"replicas": []}, "mcue_attributes_invalid"),
                         ({"replicas": ["https://example.com"]}, "dependency_reference_invalid"),
                         ({"shards": [{"store": "bad"}]}, "dependency_reference_invalid"),
                         ({"replicas": None}, "mcue_attributes_invalid")]:
        with pytest.raises(GripError, match=error):
            await update(client, target, patch)
        assert (await client.describe(target))["resource"]["metadata"] == metadata
    assert (await client.describe(host))["resource"]["metadata"]["dependencies"] is None
    assert not repo.runs


@pytest.mark.parametrize("schema", [
    {"properties": {"host": {"type": "string", "x-loom-dependency": True}}},
    {"properties": {"host": {"type": "integer", "description": "Host", "x-loom-dependency": True}}},
    {"properties": {"hosts": {"type": "array", "items": True, "description": "Hosts", "x-loom-dependency": True}}},
    {"$defs": {"host": {"type": "string", "description": "Host", "x-loom-dependency": True}}},
    {"allOf": [{"properties": {"host": {"type": "string", "description": "Host", "x-loom-dependency": True}}}]},
])
async def test_invalid_dependency_declarations_are_rejected(mcue_client, schema):
    from loom_orchestration.mcue import McueDefinition

    client, _ = mcue_client
    payload = {"mcue": "test/dependency", "version": "v1", "attributes_schema": {"type": "object", **schema}}
    with pytest.raises(ValueError, match="dependency_"):
        McueDefinition.model_validate(payload)
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(client.names.root, client.names.action("publish-mcue-type"), payload)


async def test_slave_dependency_survives_expiry_and_invalid_reregistration(mcue_client):
    from datetime import datetime, timedelta, timezone
    from loom_v2.contracts.agents import AgentRegistration
    from loom_orchestration.workflows import inventory

    client, repo = mcue_client
    host = await register(client, name="demo2zy")
    registration = AgentRegistration(
        role="slave", agent_id="slave-b", instance_id="original", workspace_id=repo.settings.workspace_id,
        endpoint_url="http://slave-b", protocol_version="loom.v1",
        resource_metadata={"mcue_ref": client.names.mcue("computing/slave", "v2"), "attributes": {"host_resource": host}},
    )
    lease = await repo.register_agent(registration)
    invalid = registration.model_copy(deep=True)
    invalid.instance_id = "invalid"
    invalid.resource_metadata.attributes = {}
    with pytest.raises(ValueError, match="mcue_attributes_invalid"):
        await repo.register_agent(invalid)
    agents = await repo.list_agents(repo.settings.workspace_id, role="slave")
    assert len(agents) == 1 and agents[0]["instance_id"] == lease.instance_id and agents[0]["lease_state"] == "active"
    key = (repo.settings.workspace_id, "slave", "slave-b", "original")
    repo.agents[key]["last_seen_at"] = datetime.now(timezone.utc) - timedelta(seconds=60)
    target = client.names.name("slaves", "slave-b")
    metadata = (await client.describe(target))["resource"]["metadata"]
    assert metadata["available"] is False and metadata["lease_state"] == "expired"
    assert metadata["dependencies"][0]["resource"] == host
    assert (await inventory(client))["slaves"][0]["dependencies"] == metadata["dependencies"]
    help_text = await client.invoke(target, client.names.action("how-to-use"), {})
    assert host not in str(help_text) and help_text["dependency_fields"][0]["required"] is True
