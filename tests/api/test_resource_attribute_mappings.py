import pytest

from loom_orchestration import ResourceEffect, resource_attributes
from loom_v2.contracts.types import IoContract
from loom_v2.observer.repository import ObserverRepository
from tests.support.package_fixture import action_export


async def publish_creator(repo, mapping):
    async def schema(properties):
        return await repo.put_content({"type": "object", "properties": properties}, media_type="application/schema+json")

    string = {"type": "string"}
    input_ref = await schema({"origin": string})
    output_ref = await schema({"id": string, "backend": string, "count": {"type": "integer"}})
    config_ref = await schema({"backend": string})
    contract = await repo.put_content(IoContract(input_schema_ref=input_ref, output_schema_ref=output_ref).model_dump(mode="json"),
                                      media_type="application/vnd.loom.io-contract+json")
    effect = {"effect": "create", "mcue_ref": repo.names.mcue("computing/slave", "v2"),
              "identity_path": "id", "attributes_mapping": mapping}
    descriptor = await repo.put_content({
        "schema_version": "action.v1", "resource_id": repo.names.action("create-executor"), "name": "create-executor",
        "resource_effects": [effect], "instance_config_schema_ref": config_ref.model_dump(mode="json"),
    }, media_type="application/vnd.loom.action-descriptor+json")
    package = await repo.register_workspace_package(repo.settings.workspace_id, {
        "package_id": "executor-factory", "package_version": "v1", "package_type": "service",
        "execution": {"kind": "container:http", "version": "1"},
        "body": {"image_ref": "registry.example/service@sha256:" + "a" * 64, "container_port": 8080, "health_path": "/healthz"},
        "action_exports": [action_export(descriptor.model_dump(mode="json"), contract.model_dump(mode="json"),
                                         runtime_binding={"path": "/create", "interaction": "task"})],
    })
    return package, effect


@pytest.mark.parametrize("mapping,error", [
    ({}, "resource_attributes_mapping_required"),
    ({"host_resource": {"source": "output", "path": "absent"}}, "resource_attribute_source_unavailable"),
    ({"host_resource": {"source": "output", "path": "count"}}, "resource_attribute_type_mismatch"),
    ({"host_resource": {"source": "constant", "value": 1}}, "resource_attribute_constant_invalid"),
    ({"host_resource": {"source": "constant", "value": "not-a-uri"}}, "dependency_reference_invalid"),
    ({"host_resource": {"source": "constant", "value": "grip://test/host"},
      "extra": {"source": "input", "path": "origin"}}, "resource_attribute_field_unknown"),
])
async def test_creation_package_rejects_missing_or_invalid_fill_rules(mapping, error):
    repo = ObserverRepository()
    with pytest.raises(ValueError, match=error):
        await publish_creator(repo, mapping)
    assert not await repo.list_capability_packages()


@pytest.mark.parametrize("source,path", [("input", "origin"), ("output", "backend"), ("instance_config", "backend"), ("constant", None)])
async def test_declared_mapping_materializes_registration_and_validates_final_value(source, path):
    repo = ObserverRepository()
    host = repo.names.name("resources", "vm", "host")
    binding = {"source": source, "path": path} if path else {"source": source, "value": host}
    package, effect = await publish_creator(repo, {"host_resource": binding})
    attributes = resource_attributes(effect, arguments={"origin": host}, result={"backend": host}, instance_config={"backend": host})
    assert attributes == {"host_resource": host}
    target = repo.names.name("resources", "executor", "created")
    resource = await repo.register_resource(repo.settings.workspace_id, {
        "resource_ref": {"resource_id": target}, "mcue_ref": effect["mcue_ref"], "attributes": attributes})
    assert resource.attributes == attributes
    if source == "output":
        # A provider returning an invalid URI cannot bypass registration validation.
        attributes = resource_attributes(effect, arguments={}, result={"backend": "bad"})
        with pytest.raises(ValueError, match="dependency_reference_invalid"):
            await repo.register_resource(repo.settings.workspace_id, {
                "resource_ref": {"resource_id": target + "-invalid"}, "mcue_ref": effect["mcue_ref"], "attributes": attributes})
    if source == "instance_config":
        # The MCUE dependency rule also checks config mapped into created attributes,
        # even when that config property's own schema has no dependency annotation.
        for config, error in [({}, "resource_attribute_source_missing"), ({"backend": "bad"}, "dependency_reference_invalid")]:
            with pytest.raises(ValueError, match=error):
                await repo.set_capability_desired(repo._package_ref(package), "slave-a", desired_state="running",
                                                  idempotency_key="create", instance_config=config)
            assert not await repo.list_package_activations(package)
        await repo.set_capability_desired(repo._package_ref(package), "slave-a", desired_state="running",
                                         idempotency_key="create", instance_config={"backend": host})


def test_attribute_mapping_reports_missing_values_without_business_data():
    effect = ResourceEffect(effect="create", mcue_ref="grip://test/types/example/v1", identity_path="id",
                            attributes_mapping={"owner": {"source": "output", "path": "owner.resource"}})
    with pytest.raises(ValueError, match="^resource_attribute_source_missing:owner$"):
        resource_attributes(effect, arguments={}, result={"secret": "private-fixture"})


def test_constant_attribute_mapping_preserves_local_schema_reference_validation():
    from loom_orchestration.mcue import McueDefinition
    from loom_v2.observer.resource_metadata import validate_attribute_mapping

    definition = McueDefinition(mcue="test/reference", version="v1", attributes_schema={
        "type": "object", "$defs": {"label": {"type": "string", "minLength": 3}},
        "properties": {"label": {"$ref": "#/$defs/label"}}, "required": ["label"],
    })
    effect = ResourceEffect(effect="create", mcue_ref="grip://test/types/example/v1", identity_path="id",
                            attributes_mapping={"label": {"source": "constant", "value": "okay"}})
    validate_attribute_mapping(effect, definition, {})
    effect.attributes_mapping["label"].value = "x"
    with pytest.raises(ValueError, match="resource_attribute_constant_invalid:label"):
        validate_attribute_mapping(effect, definition, {})
