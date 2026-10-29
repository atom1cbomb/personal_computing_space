import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.db.models import WorkspaceResourceRow
from loom_v2.observer.mcue_migration import backfill_resource_mcue
from loom_v2.observer.repository import ObserverRepository


async def test_slave_resource_metadata_survives_observer_restart(tmp_path):
    from loom_v2.contracts.agents import AgentRegistration

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agents.db'}")
    try:
        repo = ObserverRepository(engine)
        await repo.init_db()
        public = {"mcue_ref": repo.names.mcue("computing/slave", "v2"),
                  "attributes": {"host_resource": repo.names.name("resources", "vm", "host")}}
        await repo.register_agent(AgentRegistration(
            role="slave", agent_id="slave-b", instance_id="one", workspace_id="workspace-default",
            endpoint_url="http://slave-b", protocol_version="loom.v1", resource_metadata=public))
        restored = ObserverRepository(engine, content_store=repo.content_store)
        await restored.init_db()
        agents = await restored.list_agents("workspace-default", role="slave")
        assert agents[0]["capabilities"]["resource_metadata"] == public
        definition = await restored.get_mcue_definition("workspace-default", public["mcue_ref"])
        definition.validate_attributes(public["attributes"])
    finally:
        await engine.dispose()


@pytest.mark.parametrize("persistent", [False, True])
async def test_concurrent_attribute_updates_state_writes_and_restart(tmp_path, persistent):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcue.db'}") if persistent else None
    first = ObserverRepository(engine)
    await first.init_db()
    second = ObserverRepository(engine, content_store=first.content_store) if persistent else first
    if persistent:
        await second.init_db()
    scope = "workspace-default"
    target = first.names.name("resources", "vm", "concurrent")
    registration = {"resource_ref": {"resource_id": target}, "mcue_ref": first.names.mcue("computing/vm")}
    await first.register_resource(scope, registration)
    results = await asyncio.gather(
        first.update_resource_attributes(scope, target, expected_attributes_revision=1, patch={"description": "first"}),
        second.update_resource_attributes(scope, target, expected_attributes_revision=1, patch={"description": "second"}),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ValueError) and str(item) == "attributes_revision_conflict" for item in results) == 1
    assert sum(not isinstance(item, Exception) for item in results) == 1
    await asyncio.gather(
        first.register_resource(scope, {**registration, "state": "running"}),
        second.update_resource_attributes(scope, target, expected_attributes_revision=2, patch={"labels": {"project": "loom"}}),
    )
    if persistent:
        restored = ObserverRepository(engine, content_store=first.content_store)
        await restored.init_db()
    else:
        restored = first
    resource = await restored.get_resource(scope, target)
    assert resource.state == "running"
    assert resource.attributes_revision == 3
    assert resource.attributes["labels"] == {"project": "loom"}
    assert resource.attributes["description"] in {"first", "second"}
    definition = await restored.get_mcue_definition(scope, resource.mcue_ref)
    assert definition.mcue == "computing/vm"
    # A caller's modified object must not silently mutate the in-memory store.
    resource.attributes["labels"]["project"] = "tampered"
    assert (await restored.get_resource(scope, target)).attributes["labels"]["project"] == "loom"
    if persistent:
        async with first.sessions() as session:
            row = (await session.scalars(select(WorkspaceResourceRow))).one()
            assert row.attributes_revision == 3
            assert "attributes_revision" not in row.resource
        await engine.dispose()


async def test_custom_type_publication_survives_restart_and_conflicting_publish(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'definitions.db'}")
    repo = ObserverRepository(engine)
    await repo.init_db()
    other = ObserverRepository(engine, content_store=repo.content_store)
    await other.init_db()
    body = {"mcue": "storage/volume", "version": "v1", "attributes_schema": {"type": "object"}}
    first, second = await asyncio.gather(repo.publish_mcue_type("workspace-default", body), other.publish_mcue_type("workspace-default", body))
    assert first == second
    with pytest.raises(ValueError, match="mcue_version_conflict"):
        await other.publish_mcue_type("workspace-default", {**body, "description": "different"})
    restored = ObserverRepository(engine, content_store=repo.content_store)
    assert (await restored.get_mcue_definition("workspace-default", first)).mcue == "storage/volume"
    await engine.dispose()


async def test_explicit_backfill_is_atomic_and_preserves_identity_and_bindings(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
    repo = ObserverRepository(engine)
    await repo.init_db()
    scope = "workspace-default"
    vm = repo.names.name("resources", "vm", "existing")
    other = repo.names.name("resources", "vm", "another")
    original = {
        "resource_ref": {"resource_id": vm}, "state": "running",
        "package_ref": {"resource_id": repo.names.name("packages", "existing", "v1"), "version_or_digest": "a" * 64},
        "managed_by_ref": None,
    }
    async with repo.sessions() as session, session.begin():
        session.add(WorkspaceResourceRow(workspace_id=scope, resource_id=vm, resource=original))
        session.add(WorkspaceResourceRow(workspace_id=scope, resource_id=other, resource={**original, "resource_ref": {"resource_id": other}}))
    assignment = {"mcue_ref": repo.names.mcue("computing/vm"), "attributes": {"display_name": "Existing"}}
    with pytest.raises(ValueError, match="mcue_backfill_mapping_incomplete"):
        await backfill_resource_mcue(repo, scope, {vm: assignment})
    async with repo.sessions() as session:
        assert all("mcue_ref" not in row.resource for row in (await session.scalars(select(WorkspaceResourceRow))).all())
    mapping = {vm: assignment, other: assignment}
    assert await backfill_resource_mcue(repo, scope, mapping) == 2
    resource = await repo.get_resource(scope, vm)
    assert resource.resource_ref.resource_id == vm and resource.state == "running"
    assert resource.package_ref.model_dump(mode="json", exclude_defaults=True) == original["package_ref"]
    await repo.update_resource_attributes(scope, vm, expected_attributes_revision=1, patch={"display_name": "Edited"})
    assert await backfill_resource_mcue(repo, scope, mapping) == 0
    assert (await repo.get_resource(scope, vm)).attributes == {"display_name": "Edited"}
    await engine.dispose()
