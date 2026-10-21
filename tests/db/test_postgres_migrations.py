"""Migration transaction and process-lock tests against disposable PostgreSQL."""

import asyncio
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from alembic import command, op
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.db.migrations import _config, apply_migrations
from loom_v2.observer.repository import ObserverRepository
from loom_v2.observer.mcue_migration import backfill_resource_mcue


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_container():
    container = subprocess.check_output(
        ["docker", "run", "--detach", "--rm", "--publish", "127.0.0.1::5432",
         "--env", "POSTGRES_PASSWORD=migration-test", "postgres:16-alpine"],
        text=True,
    ).strip()
    try:
        for _ in range(100):
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True, timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("test PostgreSQL did not become ready")
        port = subprocess.check_output(
            ["docker", "port", container, "5432/tcp"], text=True,
        ).strip().rsplit(":", 1)[1]
        yield container, port
    finally:
        subprocess.run(["docker", "stop", "--time", "1", container], check=True, capture_output=True)


@pytest.fixture
async def database(postgres_container):
    container, port = postgres_container
    name = f"migration_{uuid4().hex}"
    subprocess.run(["docker", "exec", container, "createdb", "-U", "postgres", name], check=True)
    url = f"postgresql+asyncpg://postgres:migration-test@127.0.0.1:{port}/{name}"
    engine = create_async_engine(url)
    try:
        yield engine, url
    finally:
        await engine.dispose()


async def test_existing_slave_attempt_survives_action_column_migration(database):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: command.upgrade(_config("slave", sync), "004_run_execution_requests"))
        await connection.execute(text(
            "INSERT INTO slave_attempts (attempt_id, slave_id, workspace_id, operation, payload, state) "
            "VALUES ('attempt-existing', 'slave-a', 'workspace-default', 'instances-list', '{}', 'completed')"
        ))
        await apply_migrations(connection, "slave")
        await apply_migrations(connection, "slave")
        row = (await connection.execute(text(
            "SELECT action, state FROM slave_attempts WHERE attempt_id = 'attempt-existing'"
        ))).one()
        assert tuple(row) == ("instances-list", "completed")
        columns = (await connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'slave_attempts'"
        ))).scalars().all()
        assert "action" in columns
        assert "operation" not in columns


async def test_mcue_upgrade_preserves_existing_resource_and_serializes_writers(database):
    engine, _ = database
    repo = ObserverRepository(engine)
    scope = "workspace-default"
    target = repo.names.name("resources", "vm", "pre-mcue")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: command.upgrade(_config("observer", sync), "005_slave_action_name"))
        await connection.execute(text(
            "INSERT INTO workspace_resources (workspace_id, resource_id, resource) "
            "VALUES (:workspace, :target, CAST(:resource AS jsonb))"
        ), {"workspace": scope, "target": target, "resource": json.dumps({"resource_ref": {"resource_id": target}, "state": "running"})})
        await apply_migrations(connection, "observer")
        assert (await connection.execute(text("SELECT attributes_revision FROM workspace_resources"))).scalar_one() == 1
    await repo.ensure_mcue_definitions(scope)
    assert await backfill_resource_mcue(repo, scope, {target: {"mcue_ref": repo.names.mcue("computing/vm")}}) == 1
    other = ObserverRepository(engine, content_store=repo.content_store)
    await other.init_db()
    results = await asyncio.gather(
        repo.update_resource_attributes(scope, target, expected_attributes_revision=1, patch={"cpu_cores": 4}),
        other.update_resource_attributes(scope, target, expected_attributes_revision=1, patch={"cpu_cores": 8}),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) and str(result) == "attributes_revision_conflict" for result in results) == 1
    await asyncio.gather(
        repo.register_resource(scope, {"resource_ref": {"resource_id": target}, "mcue_ref": repo.names.mcue("computing/vm"), "state": "stopped"}),
        other.update_resource_attributes(scope, target, expected_attributes_revision=2, patch={"labels": {"environment": "test"}}),
    )
    persisted = await ObserverRepository(engine, content_store=repo.content_store).get_resource(scope, target)
    assert persisted.state == "stopped" and persisted.attributes_revision == 3
    assert persisted.attributes["cpu_cores"] in {4, 8}
    assert persisted.attributes["labels"] == {"environment": "test"}


@pytest.mark.parametrize("role,own_table,other_table", [
    ("observer", "runs", "slave_attempts"),
    ("slave", "slave_attempts", "runs"),
])
async def test_failed_migration_rolls_back_schema_and_version(database, monkeypatch, role, own_table, other_table):
    engine, _ = database
    execute = op.execute
    calls = 0

    def fail_after_first_ddl(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected_migration_failure")
        return execute(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(op, "execute", fail_after_first_ddl)
        with pytest.raises(RuntimeError, match="injected_migration_failure"):
            async with engine.begin() as connection:
                await apply_migrations(connection, role)
    async with engine.begin() as connection:
        tables = (await connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ))).scalars().all()
        assert tables == []
        await apply_migrations(connection, role)
    async with engine.begin() as connection:
        await apply_migrations(connection, role)
        assert (await connection.execute(text(f"SELECT to_regclass('{own_table}')"))).scalar() == own_table
        assert (await connection.execute(text(f"SELECT to_regclass('{other_table}')"))).scalar() is None
        assert (await connection.execute(text(
            f"SELECT version_num FROM loom_{role}_alembic_version"
            ))).scalar_one() == "007_activation_credentials"


@pytest.mark.parametrize("role", ["observer", "slave"])
async def test_concurrent_processes_upgrade_once(database, role):
    engine, url = database
    script = """
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from loom_v2.db.migrations import apply_migrations
async def main():
    engine = create_async_engine(os.environ['MIGRATION_TEST_URL'])
    try:
        async with engine.begin() as connection:
            await apply_migrations(connection, os.environ['MIGRATION_TEST_ROLE'])
    finally:
        await engine.dispose()
asyncio.run(main())
"""
    processes = []
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"loom:alembic:{role}"},
            )
            for _ in range(3):
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-c", script,
                    env={**os.environ, "MIGRATION_TEST_URL": url, "MIGRATION_TEST_ROLE": role},
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                ))
            async with asyncio.timeout(30):
                while True:
                    blocked = (await connection.execute(text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    ))).scalar_one()
                    if blocked == 3:
                        break
                    await asyncio.sleep(0.05)
        for process in processes:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, stderr.decode()
        async with engine.connect() as connection:
            assert (await connection.execute(text(
                f"SELECT version_num FROM loom_{role}_alembic_version"
                ))).scalars().all() == ["007_activation_credentials"]
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize('role', ['observer', 'slave'])
async def test_activation_credential_migration_preserves_packages_and_existing_selections(database, role):
    from copy import deepcopy
    from loom_v2.contracts.types import CapabilityPackageActivation
    from loom_v2.db.models import RunRow, WorkspaceCapabilityPackageRow, SlaveReplicaRow
    from tests.slave.test_container_http_runtime import _package

    engine, _ = database
    package = _package(['/v1/echo']).model_dump(mode='json')
    package['body']['credential_profile'] = 'legacy-profile'
    original = deepcopy(package)
    ref = 'grip://loom.local/workspaces/workspace-default/packages/pkg-http-runtime/v1'
    base = CapabilityPackageActivation(package_version_ref=ref, package_digest=package['package_digest'],
        target_slave='slave-a', activation_closure_version_ref='closure-1', compute_binding_ref='',
        desired_state='running', activation_revision=7, last_idempotency_key='original', activation_state='ready',
        instance_name='legacy').model_dump(mode='json', exclude={'credential_profile'})
    activations = [base, {**base, 'instance_name': 'explicit', 'credential_profile': 'keep-me'},
                   {**base, 'instance_name': 'anonymous', 'credential_profile': None}]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: command.upgrade(_config(role, sync), '006_mcue_attributes'))
        if role == 'observer':
            await connection.execute(WorkspaceCapabilityPackageRow.__table__.insert().values(
                workspace_id='workspace-default', package_ref=ref, package=package,
                capability_activations=activations, events=[]))
            await connection.execute(RunRow.__table__.insert().values(run_id='historic', task_ref='task',
                goal='existing', draft={}, state='completed', outcome={'value': 'unchanged'},
                capability_packages=[package], capability_activations=activations))
        else:
            await connection.execute(SlaveReplicaRow.__table__.insert().values(
                slave_id='slave-a', workspace_id='workspace-default', state='ready',
                capability_activations=[{**a, 'package_payload': package} for a in activations]))
        await apply_migrations(connection, role)
        await apply_migrations(connection, role)
        if role == 'observer':
            row = (await connection.execute(WorkspaceCapabilityPackageRow.__table__.select())).mappings().one()
            assert row['package'] == original
            updated = row['capability_activations']
            run = (await connection.execute(RunRow.__table__.select())).mappings().one()
            assert run['outcome'] == {'value': 'unchanged'} and run['capability_packages'] == [original]
            assert run['capability_activations'] == updated
        else:
            row = (await connection.execute(SlaveReplicaRow.__table__.select())).mappings().one()
            updated = row['capability_activations']
            assert all(a['package_payload'] == original for a in updated)
        assert [a['credential_profile'] for a in updated] == ['legacy-profile', 'keep-me', None]
        assert all(a['activation_revision'] == 7 and a['package_digest'] == original['package_digest'] for a in updated)
