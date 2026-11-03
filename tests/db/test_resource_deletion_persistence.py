import asyncio

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.contracts.errors import DomainError
from loom_v2.db.models import WorkspaceResourceRow
from loom_v2.observer.resources import ResourceRepository
from tests.db.test_postgres_migrations import database, postgres_container


@pytest.fixture(params=[False, True], ids=["memory", "sqlite"])
async def resources(request, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}") if request.param else None
    repo = ResourceRepository(engine)
    await repo.init_db()
    scope = repo.settings.workspace_id
    target = repo.names.name("resources", "vm")
    await repo.register_resource(scope, {
        "resource_ref": {"resource_id": target}, "mcue_ref": repo.names.mcue("computing/vm"),
        "state": "running", "attributes": {"display_name": "Keep on failure"},
    })
    try:
        yield repo, scope, target
    finally:
        if engine is not None:
            await engine.dispose()


async def test_failed_soft_delete_preserves_persisted_record_and_force_delete_survives_restart(resources):
    repo, scope, target = resources
    before = await repo.get_resource(scope, target)
    with pytest.raises(DomainError, match="resource_not_deleted"):
        await repo.delete_resource(scope, target)
    assert await repo.get_resource(scope, target) == before
    await repo.delete_resource(scope, target, force=True)
    assert (scope, target) not in repo.workspace_resources
    restored = ResourceRepository(repo.engine, content_store=repo.content_store) if repo.engine is not None else repo
    assert await restored.list_resources(scope) == []
    with pytest.raises(KeyError):
        await restored.get_resource(scope, target)
    if repo.sessions is not None:
        async with repo.sessions() as session:
            assert await session.scalar(select(WorkspaceResourceRow.resource_id)) is None


async def test_deleted_state_is_checked_from_current_record(resources):
    repo, scope, target = resources
    other = ResourceRepository(repo.engine, content_store=repo.content_store) if repo.engine is not None else repo
    await other.register_resource(scope, {
        "resource_ref": {"resource_id": target}, "mcue_ref": repo.names.mcue("computing/vm"),
        "state": "deleted",
    })
    await repo.delete_resource(scope, target)
    assert await other.list_resources(scope) == []
    # A freshly registered identity has normal attributes and revision again.
    created = await repo.register_resource(scope, {
        "resource_ref": {"resource_id": target}, "mcue_ref": repo.names.mcue("computing/vm"),
        "attributes": {"display_name": "New entity"},
    })
    assert created.attributes_revision == 1 and created.state == "unknown"
    assert created.attributes == {"display_name": "New entity"}


async def test_concurrent_deletes_have_one_success(resources):
    repo, scope, target = resources
    other = ResourceRepository(repo.engine, content_store=repo.content_store) if repo.engine is not None else repo
    results = await asyncio.gather(
        repo.delete_resource(scope, target, force=True),
        other.delete_resource(scope, target, force=True),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, KeyError) for result in results) == 1
    assert await repo.list_resources(scope) == []


async def test_database_delete_failure_rolls_back_and_keeps_cache(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
    repo = ResourceRepository(engine)
    await repo.init_db()
    scope = repo.settings.workspace_id
    target = repo.names.name("resources", "vm")
    await repo.register_resource(scope, {
        "resource_ref": {"resource_id": target}, "mcue_ref": repo.names.mcue("computing/vm"), "state": "deleted",
    })
    before = await repo.get_resource(scope, target)

    def fail_delete(connection, cursor, statement, parameters, context, many):
        if statement.startswith("DELETE FROM workspace_resources"):
            raise RuntimeError("database unavailable")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_delete)
    try:
        with pytest.raises(RuntimeError, match="database unavailable"):
            await repo.delete_resource(scope, target)
        assert repo.workspace_resources[(scope, target)] == before
        assert await repo.get_resource(scope, target) == before
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_delete)
        await engine.dispose()


@pytest.mark.integration
async def test_postgres_deletion_serializes_writers_and_preserves_children(database):
    engine, _ = database
    repo = ResourceRepository(engine)
    await repo.init_db()
    other = ResourceRepository(engine, content_store=repo.content_store)
    scope = repo.settings.workspace_id
    parent = repo.names.name("resources", "manager")
    child = repo.names.name("resources", "vm")
    await repo.register_resource(scope, {
        "resource_ref": {"resource_id": parent}, "mcue_ref": repo.names.mcue("computing/vm"),
        "state": "deleted",
    })
    before = await repo.register_resource(scope, {
        "resource_ref": {"resource_id": child}, "mcue_ref": repo.names.mcue("computing/vm"),
        "managed_by_ref": {"resource_id": parent}, "state": "running",
    })
    with pytest.raises(DomainError, match="resource_not_deleted"):
        await repo.delete_resource(scope, child)
    results = await asyncio.gather(repo.delete_resource(scope, parent), other.delete_resource(scope, parent),
                                   return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, KeyError) for result in results) == 1
    assert await other.get_resource(scope, child) == before
    await other.delete_resource(scope, child, force=True)
    restored = ResourceRepository(engine, content_store=repo.content_store)
    assert await restored.list_resources(scope) == []
