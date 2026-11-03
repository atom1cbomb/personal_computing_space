"""Production load failures reproduced against independent PostgreSQL clients."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.pool.native_tasks import NativeTasks
from loom_v2.pool.repository import PoolRepository
from tests.pool.test_concurrency import pools, postgres_container, register, submit, claim
from tests.db.test_health_history_isolation import service

pytestmark = pytest.mark.integration


async def test_submission_read_does_not_wait_for_claim_writer(pools, monkeypatch):
    writer, reader = pools
    lease = await register(writer, "claim-driver")
    item = await submit(writer, "lookup-during-claim")
    entered, release = asyncio.Event(), asyncio.Event()
    original = writer._evaluate_readiness

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(writer, "_evaluate_readiness", paused)
    native = NativeTasks(SimpleNamespace(repo=reader, settings=reader.settings, names=reader.names))
    task = asyncio.create_task(claim(writer, lease))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        view = await asyncio.wait_for(native.submission(item.request_id), 1)
        assert view["execution_request_state"] == "queued"
        assert view["current_run_state"] == "committed"
    finally:
        release.set()
        await task
    view = await native.submission(item.request_id)
    assert view["execution_request_state"] == "claimed"
    assert view["current_run_state"] == "running"


async def test_driver_commands_share_transaction_connections_at_pool_limit(pools):
    original, _ = pools
    lease = await register(original, "small-pool-driver")
    engine = create_async_engine(original.engine.url, pool_size=2, max_overflow=0, pool_timeout=0.3)
    repo = PoolRepository(engine, content_store=original.content_store)
    barrier = asyncio.Barrier(2)

    async def read(index):
        async with repo._execution_transaction(f"connection-{index}"):
            await barrier.wait()
            agents = await repo.list_agents(lease.workspace_id, "driver")
            record = await repo._agent_record(lease.workspace_id, "driver", lease.agent_id, lease.instance_id)
            assert agents[0]["lease_state"] == record["lease_state"] == "active"
            await repo.refresh_slaves(lease.workspace_id)
            await barrier.wait()

    try:
        await asyncio.wait_for(asyncio.gather(read(1), read(2)), 3)
        await repo.heartbeat_agent(lease.agent_id, lease.instance_id, lease.lease_id, lease.epoch,
                                   workspace_id=lease.workspace_id)
    finally:
        await engine.dispose()


async def test_nested_agent_reads_do_not_commit_the_callers_transaction(pools):
    from sqlalchemy import select
    from loom_v2.db.models import RunRow

    repo, reader = pools
    item = await submit(repo, "rollback-after-agent-read")
    with pytest.raises(RuntimeError, match="rollback"):
        async with repo._execution_transaction(item.run_id):
            record = await repo.get_run(item.run_id)
            record.goal = "must-not-commit"
            await repo._persist(record)
            await repo.list_agents("workspace-default")
            raise RuntimeError("rollback")
    async with reader.sessions() as session:
        assert await session.scalar(select(RunRow.goal).where(RunRow.run_id == item.run_id)) == "concurrent task"


async def test_package_reads_are_targeted_and_cache_cannot_hide_changes(pools, monkeypatch):
    original, cold = pools
    first = await service(original, "cache-target")
    other = await service(original, "cache-other")
    from loom_v2.contracts.types import CapabilityPackageVersion
    from loom_v2.db.models import WorkspaceCapabilityPackageRow

    decoded = []
    decode = CapabilityPackageVersion.from_record

    def recording(cls, value):
        decoded.append(value["package_id"])
        return decode(value)

    monkeypatch.setattr(CapabilityPackageVersion, "from_record", classmethod(recording))
    one = await cold.get_capability_package(first.version_ref)
    one.body["image_ref"] = "mutated-local-copy"
    assert (await cold.get_capability_package(first.version_ref)).body == first.body
    assert decoded == ["cache-target"]

    # Unrelated malformed records must not be decoded during a targeted read.
    async with original._execution_write_session() as session:
        row = await session.get(WorkspaceCapabilityPackageRow, ("workspace-default", other.version_ref))
        row.package = {**row.package, "package_digest": "f" * 64}
    assert (await cold.get_capability_package(first.version_ref)).package_digest == first.package_digest

    activations = original.workspace_package_activations[("workspace-default", first.version_ref)]
    activations[0].desired_state = "stopped"
    activations[0].activation_revision += 1
    await original._persist_workspace_package(first, activations=activations)
    await cold.get_capability_package(first.version_ref)
    current = cold.workspace_package_activations[("workspace-default", first.version_ref)][0]
    assert current.desired_state == "stopped" and current.activation_revision == activations[0].activation_revision

    # Lifecycle fields are excluded from package_digest, but must refresh.
    first.publication_state = "abandoned"
    await original._persist_workspace_package(first, activations=activations)
    await cold._load_workspace_packages(package_ref=first.version_ref)
    assert cold.workspace_packages[("workspace-default", first.version_ref)].publication_state == "abandoned"
    assert decoded == ["cache-target", "cache-target"]
    # Even a previously cached digest cannot make changed manifest bytes trusted.
    async with original._execution_write_session() as session:
        row = await session.get(WorkspaceCapabilityPackageRow, ("workspace-default", first.version_ref))
        row.package = {**row.package, "body": {**row.package["body"], "image_ref": "registry.example/changed@sha256:" + "b" * 64}}
    with pytest.raises(ValueError, match="package_digest_mismatch"):
        await cold._load_workspace_packages(package_ref=first.version_ref)
