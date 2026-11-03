"""The polling and catalog hot paths must stay independent of Run history."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, update, func

from loom_v2.contracts.agents import DriverCommand
from loom_v2.contracts.types import CapabilityPackageVersion, CapabilityPackageActivation, CapabilityHealthReport
from loom_v2.db.models import RunRow, RunEventRow, RunCapabilityStateRow, DriverRequestRow, RuntimeAgentRow, WorkspaceCapabilityPackageRow
from loom_v2.observer.repository import ObserverRepository
from tests.pool.test_concurrency import pools, postgres_container, register, submit
from tests.db.test_health_history_isolation import service

pytestmark = pytest.mark.integration


def status_command(lease, run_id, **changes):
    return DriverCommand(driver_id=lease.agent_id, instance_id=lease.instance_id,
        lease_id=lease.lease_id, driver_epoch=lease.epoch, request_id="repeated-status",
        command="run.status", arguments={"workspace_id": lease.workspace_id, "run_id": run_id}).model_copy(update=changes)


async def test_status_poll_is_fresh_read_only_and_skips_locked_history(pools, monkeypatch):
    writer, reader = pools
    lease = await register(writer, "poll-driver")
    item = await submit(writer, "poll-run")
    command = status_command(lease, item.run_id)
    # The cached full Run deliberately remains stale for the second poll.
    await reader.get_run(item.run_id)
    statements = []

    def capture(conn, cursor, sql, params, context, many):
        statements.append(sql.lower())

    event.listen(reader.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert (await reader.execute_driver_command(command))["state"] == "committed"
        assert len(statements) == 2
        assert all(s.lstrip().startswith("select") for s in statements)
        assert not any(token in s for s in statements for token in
            ("run_events", "runs.draft", "runs.closure_contract", "runs.outcome", "for update", "pg_advisory", "driver_requests"))
    finally:
        event.remove(reader.engine.sync_engine, "before_cursor_execute", capture)

    async with writer.sessions.begin() as session:
        await session.execute(update(RunRow).where(RunRow.run_id == item.run_id).values(state="cancelled", draft={"invalid": True}))
        await session.execute(update(RunEventRow).where(RunEventRow.run_id == item.run_id).values(event={"invalid": True}))
    async with writer.sessions.begin() as session:
        await session.get(RunRow, item.run_id, with_for_update=True)
        result = await asyncio.wait_for(reader.execute_driver_command(command), 1)
    assert result == {"run_id": item.run_id, "state": "cancelled"}
    assert reader.runs[item.run_id].state == "committed"
    assert reader._driver_requests == {} and reader._driver_request_locks == {}
    async with reader.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(DriverRequestRow)) == 0


async def test_status_poll_checks_lease_and_workspace_without_mutating_agents(pools):
    writer, reader = pools
    lease = await register(writer, "scoped-poll-driver")
    item = await submit(writer, "scoped-poll-run")
    command = status_command(lease, item.run_id)
    for changes in ({"lease_id": "wrong"}, {"driver_epoch": lease.epoch + 1}, {"instance_id": "wrong"}):
        with pytest.raises(ValueError, match="stale_driver_epoch"):
            await reader.execute_driver_command(command.model_copy(update=changes))
    async with writer.sessions.begin() as session:
        await session.execute(update(RunCapabilityStateRow).where(RunCapabilityStateRow.run_id == item.run_id).values(workspace_id="other-workspace"))
    with pytest.raises(ValueError, match="workspace_binding_mismatch"):
        await reader.execute_driver_command(command)
    async with writer.sessions.begin() as session:
        await session.execute(update(RuntimeAgentRow).values(last_seen_at=datetime.now(timezone.utc) - timedelta(days=1)))
    with pytest.raises(ValueError, match="stale_driver_epoch"):
        await reader.execute_driver_command(command)
    async with reader.sessions() as session:
        assert await session.scalar(select(RuntimeAgentRow.lease_state)) == "active"


async def private_package(repo, run_id, template, package_id="private-only"):
    run = await repo.open_run(run_id, run_id, "private capabilities")
    package = CapabilityPackageVersion.model_validate({**template.model_dump(mode="json"),
        "package_id": package_id, "scope": "run_bound", "publication_state": "candidate", "source_run_ref": run_id})
    run.capability_packages.append(package)
    run.capability_activations.append(CapabilityPackageActivation(
        package_version_ref=package.version_ref, package_digest=package.package_digest, target_slave="slave-a",
        activation_state="ready", desired_state="running", activation_revision=1,
        activation_closure_version_ref="", compute_binding_ref="", last_idempotency_key="private-activation"))
    await repo._persist(run)
    return package


async def test_catalog_uses_only_workspace_or_one_run_index_even_with_warm_cache(pools):
    writer, reader = pools
    shared = await service(writer, "shared-scope")
    first = await private_package(writer, "private-first", shared)
    second = await private_package(writer, "private-second", shared, "second-only")
    await reader.get_capability_package(second.version_ref, run_id=second.source_run_ref)
    statements = []

    def capture(conn, cursor, sql, params, context, many):
        statements.append(sql.lower())
        assert "from runs" not in sql.lower() and "run_events" not in sql.lower()

    event.listen(reader.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await reader.refresh_slaves()
        assert [p.version_ref for p in await reader.list_capability_packages()] == [shared.version_ref]
        assert [p.version_ref for p, _ in await reader.list_published_package_states("workspace-default")] == [shared.version_ref]
        assert not any("run_capability_states" in s for s in statements)
        assert (await reader.get_capability_package(first.version_ref, run_id=first.source_run_ref)).source_run_ref == first.source_run_ref
        assert len(await reader.list_package_activations(first)) == 1
        for scope in (None, first.source_run_ref):
            with pytest.raises(KeyError):
                await reader.get_capability_package(second.version_ref, run_id=scope)
        for sql in statements:
            if "run_capability_states" in sql:
                assert "where run_capability_states.run_id =" in sql
        assert reader.runs == {}
    finally:
        event.remove(reader.engine.sync_engine, "before_cursor_execute", capture)


async def test_run_capability_index_rolls_back_with_its_run(pools):
    writer, reader = pools
    shared = await service(writer, "rollback-shared")
    package = await private_package(writer, "rollback-private", shared)
    with pytest.raises(RuntimeError, match="rollback"):
        async with writer._execution_transaction(package.source_run_ref):
            run = await writer.get_run(package.source_run_ref)
            run.capability_activations[0].activation_state = "failed"
            await writer._persist(run)
            raise RuntimeError("rollback")
    assert (await reader.list_package_activations(package))[0].activation_state == "ready"
    assert (await reader.get_run(package.source_run_ref)).capability_activations[0].activation_state == "ready"


async def test_promotion_persists_workspace_package_without_sharing_private_activation(pools):
    writer, reader = pools
    shared = await service(writer, "promotion-template")
    candidate = await private_package(writer, "promotion-owner", shared)
    await writer.cancel_run(candidate.source_run_ref)
    observer = ObserverRepository(writer.engine, content_store=writer.content_store,
                                  action_descriptor_registry=writer.action_descriptor_registry)
    published = await observer.promote_capability_package(candidate.version_ref,
        run_id=candidate.source_run_ref, approved_digest=candidate.package_digest)
    assert published.scope == "workspace_reusable" and published.workspace_id == "workspace-default"
    assert await reader.get_capability_package(published.version_ref) == published
    assert await reader.list_package_activations(published) == []
    assert len(await reader.list_package_activations(candidate)) == 1
    async with reader.sessions() as session:
        row = await session.get(WorkspaceCapabilityPackageRow, (published.workspace_id, published.version_ref))
        assert row.package["source_run_ref"] == candidate.source_run_ref
    # A second cold Observer retries against durable workspace identity.
    repeated = await ObserverRepository(writer.engine, content_store=writer.content_store,
        action_descriptor_registry=writer.action_descriptor_registry).promote_capability_package(
        candidate.version_ref, run_id=candidate.source_run_ref, approved_digest=candidate.package_digest)
    assert repeated == published


async def test_migration_indexes_private_and_moves_published_packages_without_rewriting_history(pools):
    from alembic import command
    from loom_v2.db.migrations import _config, apply_migrations
    writer, reader = pools
    template = await service(writer, "migration-template")
    candidate = await private_package(writer, "migration-owner", template)
    owner = await writer.get_run(candidate.source_run_ref)
    published = candidate.model_copy(update={"package_version": "v1-reusable", "scope": "workspace_reusable",
        "publication_state": "published"})
    owner.capability_packages.append(published)
    owner.capability_activations.append(owner.capability_activations[0].model_copy(update={"package_version_ref": published.version_ref}))
    await writer._persist(owner)
    async with writer.engine.begin() as connection:
        await connection.run_sync(lambda conn: command.downgrade(_config("observer", conn), "010_driver_pool"))
        before = (await connection.execute(select(RunRow.__table__).where(RunRow.run_id == owner.run_id))).mappings().one()
        await apply_migrations(connection, "observer")
        after = (await connection.execute(select(RunRow.__table__).where(RunRow.run_id == owner.run_id))).mappings().one()
        assert before == after
    assert (await reader.get_capability_package(published.version_ref)).package_digest == candidate.package_digest
    assert len(await reader.list_package_activations(published)) == 1
    assert len(await reader.list_package_activations(candidate)) == 1
    with pytest.raises(KeyError):
        await reader.get_capability_package(candidate.version_ref)
    indexed = await reader.list_capability_packages(run_id=owner.run_id)
    assert [p.version_ref for p in indexed].count(published.version_ref) == 1
