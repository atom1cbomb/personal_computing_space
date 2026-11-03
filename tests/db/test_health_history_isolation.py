"""Health checks and lease recovery must not deserialize unrelated history."""
from datetime import datetime, timedelta, timezone
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.contracts.agents import AgentRegistration, DriverCommand
from loom_v2.contracts.types import CapabilityHealthReport
from loom_v2.db.models import WorkspaceCapabilityPackageRow
from loom_v2.pool.repository import PoolRepository as ObserverRepository
from tests.support.package_fixture import action_export, register_test_descriptor
from tests.db.test_postgres_migrations import postgres_container  # Shared disposable Docker fixture.


async def service(repo, name):
    contract = await repo.put_content({"schema_version": "io.v1"}, media_type="application/vnd.loom.io-contract+json")
    descriptor = register_test_descriptor(repo, repo.names.name("actions", name, "read"))
    package = await repo.register_workspace_package("workspace-default", {
        "package_type": "service", "package_id": name, "package_version": "v1",
        "execution": {"kind": "container:http", "version": "1"},
        "action_exports": [action_export(descriptor, contract, runtime_binding={"path": "/v1/read"})],
        "body": {"image_ref": "registry.example/service@sha256:" + "a" * 64,
                 "container_port": 8080, "health_path": "/healthz"},
    })
    await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running",
                                      idempotency_key="start-" + name)
    return package


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def history(tmp_path, request):
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"
    else:
        container, port = request.getfixturevalue("postgres_container")
        database = "health_" + uuid4().hex
        subprocess.run(["docker", "exec", container, "createdb", "-U", "postgres", database], check=True)
        url = f"postgresql+asyncpg://postgres:migration-test@127.0.0.1:{port}/{database}"
    engine = create_async_engine(url)
    repo = ObserverRepository(engine)
    await repo.init_db()
    for name in ("closed-history", "completed-history"):
        record = await repo.open_run(name, name, "Historical computation")
        record.state = "closed" if name.startswith("closed") else "completed"
        record.outcome = {"value": {"large_output": "x" * 128_000}}
        await repo._persist(record)
    target = await service(repo, "health-target")
    other = await service(repo, "other-service")
    for package in (target, other):
        key = ("workspace-default", package.version_ref)
        events = [{"phase": "preserved-audit", "payload": "old history" * 1000}]
        await repo._persist_workspace_package(package, events=events)
    try:
        yield repo, target, other
    finally:
        await engine.dispose()


def cold_repository(repo):
    return ObserverRepository(repo.engine, content_store=repo.content_store,
                              action_descriptor_registry=repo.action_descriptor_registry)


def report(package, report_id="healthy"):
    return CapabilityHealthReport(report_id=report_id, package_version_ref=package.version_ref,
        package_digest=package.package_digest, target_slave="slave-a",
        activation_revision=1, activation_state="ready")


async def test_health_history_cannot_expire_an_unrelated_driver_lease(history, monkeypatch):
    """A slow historical decode used to run in a health request and lose the lease."""
    import loom_v2.observer.resources as repository_module

    original, package, _ = history
    repo = cold_repository(original)

    class Clock(datetime):
        current = datetime(2026, 10, 31, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(repository_module, "datetime", Clock)
    decoding_history = False
    decoded = []
    decode = repo._record_from_row

    def slow_history(row):
        if decoding_history and row.state in {"closed", "completed"}:
            decoded.append(row.run_id)
            Clock.current += timedelta(seconds=20)  # Deliberately exceeds the 15s lease.
        return decode(row, events)

    monkeypatch.setattr(repo, "_record_from_row", slow_history)
    lease = await repo.register_agent(AgentRegistration(role="driver", agent_id="driver-default",
        instance_id="one-driver", workspace_id="workspace-default", endpoint_url="http://driver:8090",
        protocol_version="loom.v1"))
    decoding_history = True
    ready = await repo.record_capability_health(report(package))
    renewed = await repo.heartbeat_agent(lease.agent_id, lease.instance_id, lease.lease_id,
                                        lease.epoch, workspace_id="workspace-default")
    assert ready.activation_state == "ready"
    assert renewed.epoch == lease.epoch
    assert decoded == []


async def test_health_reads_only_waiting_runs_and_preserves_audit_and_idempotency(history, monkeypatch):
    original, package, other = history
    waiting = await original.open_run("waiting-on-other-service", "waiting", "Unrelated authentication")
    waiting.state = "awaiting_auth"
    waiting.events.append({"phase": "activation_wait", "activation_ref": {
        "version_or_digest": other.package_digest,
        "access_binding": {"package_version_ref": other.version_ref, "target_slave": "slave-a",
                           "instance_name": "default", "activation_revision": 1},
    }})
    await original._persist(waiting)
    repo = cold_repository(original)
    decoded = []
    decode = repo._record_from_row

    def observe(row, events):
        decoded.append(row.run_id)
        return decode(row, events)

    monkeypatch.setattr(repo, "_record_from_row", observe)
    statements = []
    listener = lambda conn, cursor, sql, params, context, many: statements.append(sql)
    event.listen(repo.engine.sync_engine, "before_cursor_execute", listener)
    try:
        await repo.record_capability_health(report(package))
    finally:
        event.remove(repo.engine.sync_engine, "before_cursor_execute", listener)
    assert decoded == [waiting.run_id]
    history_reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")
                     and "workspace_capability_packages.events" in s]
    assert history_reads == []
    assert repo.workspace_package_events == {}

    await repo.record_capability_health(report(package))
    with pytest.raises(ValueError, match="health_report_id_reused"):
        await repo.record_capability_health(report(package).model_copy(update={"activation_state": "degraded"}))
    await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="stopped",
                                      idempotency_key="stop-target")
    await repo.abandon_capability_package(package.version_ref)
    saved = {p.version_ref: await repo.read_package_events(p) for p in (package, other)}
    target_events = saved[package.version_ref]
    assert target_events[1]["phase"] == "preserved-audit"
    assert [e["phase"] for e in target_events[2:]] == [
        "capability_health_report", "capability_activation_superseded",
        "capability_desired_state_changed", "capability_package_abandoned"]
    assert saved[other.version_ref] == await original.read_package_events(other)
    assert (await repo.get_run(waiting.run_id)).state == "awaiting_auth"


async def test_driver_registration_and_recovery_skip_terminal_run_payloads(history, monkeypatch):
    original, _, _ = history
    live = await original.open_run("live-recovery", "live", "Recover current work")
    live.state = "thinking"
    await original._persist(live)
    repo = cold_repository(original)
    decoded = []
    decode = repo._record_from_row

    def observe(row, events):
        decoded.append(row.run_id)
        return decode(row, events)

    monkeypatch.setattr(repo, "_record_from_row", observe)
    lease = await repo.register_agent(AgentRegistration(role="driver", agent_id="driver-default",
        instance_id="new-driver", workspace_id="workspace-default", endpoint_url="http://driver:8090",
        protocol_version="loom.v1"))
    assert lease.recovery_runs == []  # Registration does not claim or decode another Driver's Runs.
    recovered = await repo.execute_driver_command(DriverCommand(request_id="recovery-list",
        driver_id=lease.agent_id, instance_id=lease.instance_id, lease_id=lease.lease_id,
        driver_epoch=lease.epoch, command="run.recovery.list", arguments={"workspace_id": "workspace-default"}))
    assert [r["run_id"] for r in recovered["runs"]] == [live.run_id]
    assert decoded == [live.run_id]


async def test_catalog_and_startup_never_read_run_execution_history(history):
    original, package, _ = history
    from loom_v2.contracts.types import CapabilityPackageVersion
    owner = await original.get_run('closed-history')
    legacy = CapabilityPackageVersion.model_validate({**package.model_dump(mode='json'),
        'package_id': 'legacy-run-package', 'package_digest': '', 'workspace_id': None,
        'scope': 'run_bound', 'source_run_ref': owner.run_id})
    owner.capability_packages.append(legacy)
    await original._persist(owner)
    repo = cold_repository(original)
    statements = []
    def check(conn, cursor, sql, params, context, many):
        if sql.lstrip().upper().startswith('SELECT'):
            statements.append(sql)
            assert 'runs.outcome' not in sql and 'runs.events' not in sql
            assert 'workspace_capability_packages.events' not in sql
    event.listen(repo.engine.sync_engine, 'before_cursor_execute', check)
    try:
        for _ in range(2):
            await repo.refresh_slaves()
            await repo.list_capability_packages()
            assert (await repo.get_capability_package(legacy.version_ref, run_id=owner.run_id)).package_digest == legacy.package_digest
            await repo.get_capability_package_aggregate(legacy.version_ref, run_id=legacy.source_run_ref)
            await repo.list_package_activations(legacy)
            await repo.recover_persisted_activation_waits()
            await repo.recover_stale_runs()
            await repo.list_conversations()
        async with repo._execution_transaction():
            await repo.refresh_slaves()
        assert repo.runs == {}
    finally:
        event.remove(repo.engine.sync_engine, 'before_cursor_execute', check)
    assert statements
    actual_owner = await repo._activation_record(legacy)
    assert actual_owner.run_id == owner.run_id
    assert set(repo.runs) == {owner.run_id}


async def test_settlement_ignores_other_runs_request_bodies(history, monkeypatch):
    from loom_v2.repository_state import ExecutionRequest
    original, _, _ = history
    target = await original.open_run('settlement-target', 'target', 'settle just this run')
    target.state = 'completed'
    await original._persist(target)
    for run_id in ('closed-history', 'completed-history', target.run_id):
        await original._persist_execution_request(ExecutionRequest(
            workspace_id='workspace-default', request_id=run_id, run_id=run_id,
            closure_version='v1', request_payload={'large': 'x' * 128_000},
            state='queued' if run_id == target.run_id else 'settled', outcome={'large': 'y' * 128_000}))
    repo = cold_repository(original)
    decoded = []
    decode = repo._execution_request_from_row
    def observe(row):
        decoded.append(row.run_id)
        assert row.run_id == target.run_id
        return decode(row)
    monkeypatch.setattr(repo, '_execution_request_from_row', observe)
    await repo._settle_execution_request(target.run_id, outcome={'ok': True})
    assert decoded == [target.run_id]
    saved = await cold_repository(original).get_execution_request('workspace-default', target.run_id)
    assert saved.state == 'settled' and saved.outcome == {'ok': True}
    old = await original.get_execution_request('workspace-default', 'closed-history')
    assert old.outcome == {'large': 'y' * 128_000}


async def test_audit_append_is_atomic_and_paged(history):
    original, package, _ = history
    repo = cold_repository(original)
    before = await repo.read_package_events(package)
    with pytest.raises(RuntimeError, match='rollback-audit'):
        async with repo._execution_transaction():
            await repo.record_capability_health(report(package))
            # Nested operations must observe the current transaction's writes.
            repeated = await repo.record_capability_health(report(package))
            assert repeated.activation_state == 'ready'
            raise RuntimeError('rollback-audit')
    restored = cold_repository(original)
    assert await restored.read_package_events(package) == before
    await restored.record_capability_health(report(package))
    await restored.record_capability_health(report(package, 'next-report'))
    page_one = await restored.read_package_events(package, limit=2)
    page_two = await restored.read_package_events(package, after=2, limit=2)
    assert page_one == before
    assert [e['report_id'] for e in page_two] == ['healthy', 'next-report']
    async with repo.sessions() as session:
        old = await session.scalar(select(WorkspaceCapabilityPackageRow.events).where(
            WorkspaceCapabilityPackageRow.package_ref == package.version_ref))
    assert old == []  # Newly appended audit never rewrites the legacy column.


async def test_postgres_audit_upgrade_downgrade_preserves_reports(history):
    from alembic import command
    from loom_v2.db.migrations import _config
    original, package, _ = history
    if original.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL schema migration')
    await original.record_capability_health(report(package))
    before = await original.read_package_events(package)
    async with original.engine.begin() as connection:
        await connection.run_sync(lambda c: command.downgrade(_config('observer', c), '008_resource_access_bindings'))
        legacy = (await connection.execute(select(WorkspaceCapabilityPackageRow.events).where(
            WorkspaceCapabilityPackageRow.package_ref == package.version_ref))).scalar_one()
        assert legacy == before
        await connection.run_sync(lambda c: command.upgrade(_config('observer', c), 'head'))
    repo = cold_repository(original)
    await repo.record_capability_health(report(package))
    assert await repo.read_package_events(package) == before  # Pre-upgrade report id remains idempotent.
    await repo.record_capability_health(report(package, 'post-upgrade'))
    async with original.engine.begin() as connection:
        await connection.run_sync(lambda c: command.downgrade(_config('observer', c), '008_resource_access_bindings'))
        legacy = (await connection.execute(select(WorkspaceCapabilityPackageRow.events).where(
            WorkspaceCapabilityPackageRow.package_ref == package.version_ref))).scalar_one()
        assert legacy == before + [dict(legacy[-1])]
        assert legacy[-1]['report_id'] == 'post-upgrade'
        await connection.run_sync(lambda c: command.upgrade(_config('observer', c), 'head'))
    assert await cold_repository(original).read_package_events(package) == legacy


async def test_postgres_concurrent_audit_preserves_idempotency_and_order(history):
    import asyncio
    original, package, _ = history
    if original.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL cross-process transaction serialization')
    before = await original.read_package_events(package)
    first, second = cold_repository(original), cold_repository(original)
    await asyncio.gather(first.record_capability_health(report(package, 'duplicate')),
                         second.record_capability_health(report(package, 'duplicate')))
    after_duplicate = await original.read_package_events(package)
    assert after_duplicate[:-1] == before
    assert after_duplicate[-1]['report_id'] == 'duplicate'
    await asyncio.gather(first.record_capability_health(report(package, 'first')),
                         second.record_capability_health(report(package, 'second')))
    after = await original.read_package_events(package)
    assert after[:-2] == after_duplicate
    assert {e['report_id'] for e in after[-2:]} == {'first', 'second'}
