"""PostgreSQL concurrency and ownership tests using independent Pool repositories."""
import asyncio
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import select
from loom_v2.db.session import make_engine
from loom_v2.db.models import RunEventRow
from loom_v2.pool.repository import PoolRepository
from loom_v2.contracts.agents import AgentRegistration, DriverCommand
from loom_v2.contracts.types import ClosureContract
from tests.db.test_postgres_migrations import postgres_container

pytestmark = pytest.mark.integration


@pytest.fixture
async def pools(postgres_container):
    container, port = postgres_container
    database = "pool_" + uuid4().hex
    subprocess.run(["docker", "exec", container, "createdb", "-U", "postgres", database], check=True)
    engine = make_engine(f"postgresql+asyncpg://postgres:migration-test@127.0.0.1:{port}/{database}")
    a, b = PoolRepository(engine), PoolRepository(engine)
    await a.init_db()
    await b.init_db()
    try:
        yield a, b
    finally:
        await engine.dispose()


async def register(repo, name):
    return await repo.register_agent(AgentRegistration(role="driver", agent_id=name, instance_id=name + "-instance",
        workspace_id="workspace-default", endpoint_url="http://" + name, protocol_version="loom.v1",
        capabilities={"execution": True, "orchestration": True}))


async def submit(repo, key):
    return await repo.submit_run_plan(workspace_id="workspace-default", request_id=key,
        plan=ClosureContract(closure_id=key, goal="concurrent task", body={"closure_id": key}))


async def claim(repo, lease):
    return await repo.claim_next_execution(workspace_id=lease.workspace_id, driver_id=lease.agent_id,
        driver_instance_id=lease.instance_id, driver_epoch=lease.epoch)


async def test_claims_execute_readiness_concurrently_and_never_share_a_run(pools, monkeypatch):
    a, b = pools
    la, lb = await register(a, "a"), await register(b, "b")
    await submit(a, "one")
    await submit(b, "two")
    entered = 0
    both = asyncio.Event()
    for repo in (a, b):
        original = repo._evaluate_readiness
        async def readiness(*args, _original=original, **kwargs):
            nonlocal entered
            entered += 1
            if entered >= 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)
            return await _original(*args, **kwargs)
        monkeypatch.setattr(repo, "_evaluate_readiness", readiness)
    first, second = await asyncio.gather(claim(a, la), claim(b, lb))
    assert first["run"]["run_id"] != second["run"]["run_id"]
    assert await claim(a, la) is None
    assert await claim(b, lb) is None
    assert len([x for x in await a.list_agents(la.workspace_id, "driver") if x["lease_state"] == "active"]) == 2


async def test_submission_is_atomic_across_pool_replicas(pools):
    a, b = pools
    first, second = await asyncio.gather(submit(a, "same"), submit(b, "same"))
    assert first.run_id == second.run_id
    rows = await a.list_run_summaries("workspace-default")
    assert len(rows) == 1
    async with a.sessions() as session:
        events = list((await session.scalars(select(RunEventRow).where(RunEventRow.run_id == first.run_id))).all())
    assert len(events) == len({e.sequence for e in events})


async def test_recovery_preserves_healthy_peer_and_fences_old_owner(pools):
    a, b = pools
    la, lb = await register(a, "a"), await register(b, "b")
    await submit(a, "one")
    await submit(b, "two")
    first, second = await claim(a, la), await claim(b, lb)
    recovered = await b.recover_execution_requests_for_driver(lb.workspace_id, driver_id=lb.agent_id,
        driver_instance_id=lb.instance_id, driver_epoch=lb.epoch)
    assert recovered == 0
    await a.release_agent(la.agent_id, la.instance_id, la.lease_id, la.epoch, workspace_id=la.workspace_id)
    recovered = await b.recover_execution_requests_for_driver(lb.workspace_id, driver_id=lb.agent_id,
        driver_instance_id=lb.instance_id, driver_epoch=lb.epoch)
    assert recovered == 1
    resumed = await claim(b, lb)
    assert resumed["run"]["run_id"] == first["run"]["run_id"]
    assert resumed["run"]["execution_id"] == first["run"]["execution_id"]
    history = await a.read_run_history(la.workspace_id, first["run"]["run_id"])
    ownership = [e for e in history["events"] if e.get("phase") == "execution_claimed"]
    assert [e["driver_id"] for e in ownership] == [la.agent_id, lb.agent_id]
    assert ownership[-1]["recovered"] is True
    peer = await b.get_execution_request(lb.workspace_id, second["request"]["request_id"])
    assert peer.state == "claimed" and peer.driver_id == lb.agent_id
    with pytest.raises(ValueError, match="stale_driver_epoch"):
        await a.execute_driver_command(DriverCommand(driver_id=la.agent_id, instance_id=la.instance_id,
            lease_id=la.lease_id, driver_epoch=la.epoch, request_id="late-cancel", command="run.cancel",
            arguments={"workspace_id": la.workspace_id, "run_id": first["run"]["run_id"]}))


async def test_history_survives_downgrade_and_upgrade(pools):
    from alembic import command
    from sqlalchemy import text
    from loom_v2.db.migrations import _config, apply_migrations
    a, _ = pools
    item = await submit(a, "history-migration")
    before = (await a.read_run_history("workspace-default", item.run_id, limit=1000))["events"]
    async with a.engine.begin() as connection:
        await connection.run_sync(lambda conn: command.downgrade(_config("observer", conn), "009_history_isolation"))
        legacy = await connection.scalar(text("SELECT events FROM runs WHERE run_id=:id"), {"id": item.run_id})
        assert legacy == before
        await apply_migrations(connection, "observer")
    cold = PoolRepository(a.engine)
    assert (await cold.read_run_history("workspace-default", item.run_id, limit=1000))["events"] == before
    async with cold._execution_transaction(item.run_id):
        run = await cold.get_run(item.run_id)
        run.events.append({"phase": "after-migration"})
        await cold._persist(run)
    assert (await cold.read_run_history("workspace-default", item.run_id, after=len(before)))["events"] == [{"phase": "after-migration"}]


async def test_retried_claim_is_idempotent_across_pool_replicas(pools):
    a, b = pools
    lease = await register(a, "retry-driver")
    await submit(a, "first")
    await submit(a, "second")
    command = DriverCommand(driver_id=lease.agent_id, instance_id=lease.instance_id,
        lease_id=lease.lease_id, driver_epoch=lease.epoch, request_id="same-claim",
        command="execution.claim_next", arguments={"workspace_id": lease.workspace_id})
    first, repeated = await asyncio.gather(a.execute_driver_command(command), b.execute_driver_command(command))
    assert first == repeated
    items = await b.list_execution_requests(lease.workspace_id)
    assert sorted(i.state for i in items) == ["claimed", "queued"]


async def test_concurrent_recovery_does_not_double_recover(pools):
    a, b = pools
    lease = await register(a, "expired")
    await submit(a, "recover-once")
    await claim(a, lease)
    await a.release_agent(lease.agent_id, lease.instance_id, lease.lease_id, lease.epoch, workspace_id=lease.workspace_id)
    counts = await asyncio.gather(*(r.recover_execution_requests_for_driver(lease.workspace_id,
        driver_id="", driver_instance_id="", driver_epoch=0) for r in (a, b)))
    assert sum(counts) == 1


async def test_successful_command_receipt_survives_queue_settlement(pools):
    a, b = pools
    lease = await register(a, "cancel-driver")
    submitted = await submit(a, "cancel-retry")
    await claim(a, lease)
    command = DriverCommand(driver_id=lease.agent_id, instance_id=lease.instance_id,
        lease_id=lease.lease_id, driver_epoch=lease.epoch, request_id="cancel-once",
        command="run.cancel", arguments={"workspace_id": lease.workspace_id, "run_id": submitted.run_id})
    first = await a.execute_driver_command(command)
    assert (await b.get_execution_request(lease.workspace_id, "cancel-retry")).state == "settled"
    assert await b.execute_driver_command(command) == first


async def test_pool_lifespan_recovers_without_a_connected_driver(pools):
    from loom_v2.pool.app import create_app
    a, b = pools
    lease = await register(a, "lost-driver")
    await submit(a, "background-recovery")
    await claim(a, lease)
    await a.release_agent(lease.agent_id, lease.instance_id, lease.lease_id, lease.epoch, workspace_id=lease.workspace_id)
    app = create_app(b, settings=b.settings.model_copy(update={"agent_heartbeat_interval_seconds": 0.05}))
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(5):
            while True:
                requests = await a.list_execution_requests(lease.workspace_id)
                if requests[0].state == "recovering":
                    break
                await asyncio.sleep(0.05)


async def test_pending_decision_recovery_receipt_cannot_block_ready_tasks(pools):
    a, b = pools
    lease = await register(a, "queue-driver")
    blocked = await submit(a, "legacy-pending-decision")
    async with a._execution_transaction(blocked.run_id):
        run = await a.get_run(blocked.run_id)
        run.state = "awaiting_decision"
        await a._persist(run)
        item = await a.get_execution_request(lease.workspace_id, blocked.request_id)
        item.state = "recovering"
        await a._persist_execution_request(item)
    ready = await submit(a, "new-ready")
    claimed = await claim(b, lease)
    assert claimed["run"]["run_id"] == ready.run_id
    async with a._execution_transaction(blocked.run_id):
        assert (await a.get_run(blocked.run_id)).state == "awaiting_decision"
        assert (await a.get_execution_request(lease.workspace_id, blocked.request_id)).state == "recovering"
