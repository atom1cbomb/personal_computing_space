"""Directory query cost and freshness through the unchanged public SDK."""
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import create_async_engine

from loom_orchestration import GripError, LoomClient
from loom_v2.contracts.types import CapabilityPackageActivation, CapabilityPackageVersion, ResourceRef
from loom_v2.db.models import RunRow, WorkspaceCapabilityPackageRow
from loom_v2.digest import digest_json
from loom_v2.observer.app import create_app
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from tests.support.package_fixture import action_export, register_test_descriptor


async def seed_packages(repo, count, *, start=0, exports=2, activations=1, workspace="workspace-default"):
    """Seed valid persisted state without starting any execution services."""
    schema = await repo.put_content({"type": "object", "properties": {"value": {"type": "integer"}}},
                                    media_type="application/schema+json")
    contract = await repo.put_content({"schema_version": "io.v1", "input_schema_ref": schema.model_dump(mode="json")},
                                      media_type="application/vnd.loom.io-contract+json")
    packages = []
    for i in range(start, start + count):
        package = CapabilityPackageVersion(
            package_id=f"directory-{i}", package_version="v1", workspace_id=workspace,
            scope="workspace_reusable", publication_state="published", package_type="service",
            execution={"kind": "container:http", "version": "1"},
            body={"image_ref": "example/directory@sha256:" + "a" * 64, "container_port": 8080, "health_path": "/healthz"},
            action_exports=[action_export(register_test_descriptor(
                repo, repo.names.name("actions", "directory", f"{i}-{j}"),
            ), contract, runtime_binding={"path": f"/action-{j}"}) for j in range(exports)],
        )
        states = [CapabilityPackageActivation(
            package_version_ref=package.version_ref, package_digest=package.package_digest,
            target_slave="slave-a", instance_name=f"instance-{j}",
            desired_state="running", activation_state="ready", activation_revision=1,
            activation_closure_version_ref=f"activation-{i}-{j}", compute_binding_ref=f"binding-{i}-{j}",
            last_idempotency_key=f"activate-{i}-{j}",
        ) for j in range(activations)]
        await repo._persist_workspace_package(package, activations=states)
        packages.append(package)
    return packages


@contextmanager
def queries(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.fixture
async def directory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'directory.db'}")
    settings = Settings(grip_token="directory-test", public_base_url="http://observer")
    repo = ObserverRepository(engine, settings=settings)
    await repo.init_db()
    await repo.ensure_mcue_definitions(settings.workspace_id)
    app = create_app(repo, settings=settings)
    async with LoomClient("http://observer", token=settings.grip_token, allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=app)) as client:
        yield repo, app, client, engine
    await engine.dispose()


async def test_static_describe_and_schema_read_do_not_scan_directory(directory, monkeypatch):
    repo, app, client, engine = directory

    async def no_catalog(**kwargs):
        pytest.fail("static targets must not build the dynamic directory")

    monkeypatch.setattr(app.state.grip, "catalog", no_catalog)
    content = await repo.put_content({"value": 42}, media_type="application/json")
    with queries(engine) as sql:
        root = await client.describe(client.names.root)
        action = await client.invoke(client.names.action("list-resources"), client.names.action("read"), {})
        schema = await client.invoke(action["input_schema"], client.names.action("read"), {})
        result = await client.invoke(content.resource_id, client.names.action("read"), {})
    assert root["resource"]["mcue"] == "loom/workspace"
    assert "mcue" in schema["properties"]
    assert result["content"] == {"value": 42}
    assert sql == []


async def test_list_query_count_does_not_grow_with_packages(directory, monkeypatch):
    repo, app, client, engine = directory
    await repo.open_run("history", "history", "history")

    async def no_full_load(*args, **kwargs):
        pytest.fail("directory reads must not load full histories or reload packages per activation")

    for name in ("_all_records", "_load_workspace_packages", "list_package_activations"):
        monkeypatch.setattr(repo, name, no_full_load)
    counts = []
    for start, count in ((0, 1), (1, 8)):
        await seed_packages(repo, count, start=start)
        with queries(engine) as sql:
            listed = await client.resources()
        assert len([r for r in listed if r["mcue"] == "capability/package"]) == start + count
        assert sum("FROM workspace_capability_packages" in s for s in sql) == 1
        assert all("runs.events" not in s and "runs.outcome" not in s and "runs.draft" not in s for s in sql)
        counts.append(len(sql))
    assert counts[0] == counts[1]


async def test_type_filter_skips_unrelated_sources_and_description_hashes(directory, monkeypatch):
    repo, app, client, engine = directory
    await seed_packages(repo, 3)
    await repo.open_run("history", "history", "history")

    async def unrelated(*args, **kwargs):
        pytest.fail("unrelated resource source was accessed")

    monkeypatch.setattr(repo, "list_published_package_states", unrelated)
    monkeypatch.setattr(repo, "list_run_summaries", unrelated)
    monkeypatch.setattr(app.state.grip, "_actions", unrelated)
    with queries(engine) as sql:
        assert await client.resources(mcue="computing/slave") == []
        assert await client.resources(mcue="unknown/type") == []
    assert all("FROM runs" not in s and "FROM workspace_capability_packages" not in s for s in sql)

    # A direct list reply needs no Description revision digest, even for dynamic entries.
    def no_hash(*args, **kwargs):
        pytest.fail("list-resources must not hash descriptions")

    monkeypatch.setattr("loom_v2.observer.grip.digest_json", no_hash)
    payload = client._grip_request("INVOKE", client.names.root,
                                   action=client.names.action("list-resources"), input={"mcue": "grip/mcue-type"})
    result = await client._grip_exchange(client.base_url + "/.well-known/grip", payload)
    assert len(result["resources"]) == 15


async def test_listing_and_target_descriptions_agree_and_support_registered_builtin_types(directory):
    repo, app, client, _ = directory
    await seed_packages(repo, 2)
    await repo.open_run("history", "history", "history")
    target = client.names.name("resources", "custom-schema")
    await repo.register_resource(repo.settings.workspace_id, {
        "resource_ref": {"resource_id": target}, "mcue_ref": client.names.mcue("grip/schema"),
    })
    resources = await client.resources()
    for resource in resources:
        description = await client.describe(resource["uri"])
        assert description["resource"] == resource
        revision = description.pop("revision")
        assert revision == digest_json(description, domain="loom/grip-description/v1")
    filtered = await client.resources(mcue="grip/schema")
    assert filtered == [r for r in resources if r["mcue"] == "grip/schema"]
    assert target in {r["uri"] for r in filtered}


async def test_warm_directory_observes_external_updates_and_package_visibility(directory):
    repo, app, client, engine = directory
    package, = await seed_packages(repo, 1)
    n = client.names
    target = n.name("resources", "vm", "live")
    await repo.register_resource(repo.settings.workspace_id, {
        "resource_ref": {"resource_id": target}, "mcue_ref": n.mcue("computing/vm"),
        "attributes": {"display_name": "before"},
    })
    activation, = await repo.list_package_activations(package)
    activation.target_resource_ref = ResourceRef(resource_id=target)
    await repo._persist_workspace_package(package, activations=[activation])
    activation_uri = app.state.grip.activation_uri(package, activation)
    before = await client.describe(target)
    declared = (await client.describe(package.version_ref))["resource"]["metadata"]["actions"][0]
    await client.invoke(declared["input_schema"], n.action("read"), {})
    writer = ObserverRepository(engine, content_store=repo.content_store, settings=repo.settings)
    await writer.update_resource_attributes(repo.settings.workspace_id, target, expected_attributes_revision=1,
                                             patch={"display_name": "after"})
    # Commit from another repository instance to exercise database freshness.
    stopped = activation.model_copy(update={"desired_state": "stopped", "activation_state": "stopped", "activation_revision": 2})
    await writer._persist_workspace_package(package, activations=[stopped])
    current = await client.describe(target)
    assert current["revision"] != before["revision"]
    assert current["resource"]["metadata"]["attributes"]["display_name"] == "after"
    assert current["resource"]["metadata"]["activations"] == []
    assert [o["action"]["uri"] for o in current["offers"]] == [n.action("update-attributes"), n.action("how-to-use")]
    state = await client.describe(activation_uri)
    assert state["resource"]["metadata"]["revision"] == 2
    assert not any("/exports/" in o["action"]["uri"] for o in state["offers"])
    assert (await client.resources(mcue="capability/activation"))[0] == state["resource"]
    with pytest.raises(GripError, match="unsupported_action"):
        await client.invoke(activation_uri, declared["uri"], {"arguments": {}, "expected_revision": 1, "idempotency_key": "stale"})

    abandoned = package.model_copy(update={"publication_state": "abandoned"})
    await writer._persist_workspace_package(abandoned, activations=[stopped])
    for uri in (package.version_ref, activation_uri, declared["uri"], declared["input_schema"], declared["descriptor"]["resource_id"]):
        with pytest.raises(GripError, match="not_found"):
            await client.describe(uri)
    assert await client.resources(mcue="capability/package") == []
    await seed_packages(repo, 1, start=1)
    assert len(await client.resources(mcue="capability/package")) == 1


async def test_run_projection_reads_fresh_state_without_loading_history(directory, monkeypatch):
    repo, app, client, engine = directory
    run = await repo.open_run("live", "live", "live")
    await repo.open_run("foreign", "foreign", "foreign", workspace_id="foreign")
    uri = client.names.name("runs", run.run_id)
    await client.describe(uri)
    async with repo.sessions() as session, session.begin():
        await session.execute(update(RunRow).where(RunRow.run_id == run.run_id).values(state="completed", outcome={"answer": 42}))

    async def no_load(*args, **kwargs):
        pytest.fail("run reads must not materialize execution history")

    monkeypatch.setattr(repo, "_all_records", no_load)
    monkeypatch.setattr(repo, "_load", no_load)
    with queries(engine) as sql:
        result = await client.invoke(uri, client.names.action("read"), {})
        listed = await client.resources(mcue="execution/run")
    assert result == {"state": "completed", "outcome": {"answer": 42}}
    assert [(r["uri"], r["metadata"]["state"]) for r in listed] == [(uri, "completed")]
    assert all("runs.events" not in s and "runs.draft" not in s for s in sql)
    with pytest.raises(GripError, match="not_found"):
        await client.describe(client.names.name("runs", "foreign"))


async def test_published_run_packages_and_workspace_isolation(directory):
    repo, app, client, engine = directory
    package, = await seed_packages(repo, 1)
    run = await repo.open_run("published", "published", "published")
    run.capability_packages.append(package)
    await repo._persist(run)
    async with repo.sessions() as session, session.begin():
        row = await session.get(WorkspaceCapabilityPackageRow, (repo.settings.workspace_id, package.version_ref))
        await session.delete(row)
    await seed_packages(repo, 1, start=10, workspace="foreign")
    resources = await client.resources(mcue="capability/package")
    assert [r["uri"] for r in resources] == [package.version_ref]
    assert (await client.describe(package.version_ref))["resource"] == resources[0]


async def test_shared_action_identity_tracks_current_published_revisions(directory):
    repo, app, client, _ = directory
    first, second = await seed_packages(repo, 2, exports=1, activations=0)
    identity = repo.names.name("actions", "directory", "shared")
    refs = []
    for index, package in enumerate((first, second)):
        digest = repo.register_action_descriptor(identity, {"schema_version": "action.v1", "resource_id": identity,
                                                           "name": "shared", "description": str(index)})
        ref = ResourceRef(resource_id=identity, version_or_digest=digest, identity_criterion="descriptor_digest")
        refs.append(ref.model_dump(mode="json", exclude_none=True))
        payload = package.model_dump(mode="json")
        payload["action_exports"][0]["action_descriptor_ref"] = ref.model_dump(mode="json")
        payload["package_digest"] = ""
        updated = CapabilityPackageVersion.model_validate(payload)
        await repo._persist_workspace_package(updated)
    assert (await client.invoke(identity, client.names.action("read"), {}))["descriptors"] == refs
    await repo.abandon_capability_package(second.version_ref)
    assert (await client.invoke(identity, client.names.action("read"), {}))["descriptors"] == refs[:1]
