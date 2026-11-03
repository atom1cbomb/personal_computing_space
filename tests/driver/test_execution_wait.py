"""Local and remote execution waiters share ownership/cancellation semantics."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loom_v2.coding_agents.fake import FakeCodingAgentProvider
from loom_v2.driver.mcp import DriverMCP
from loom_v2.driver.service import DriverService
from loom_v2.pool.repository import PoolRepository as ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.executor import ExecutionResult
from loom_v2.contracts.types import ResourceRef, ClosureContract
from loom_v2.content_store import canonical_json_bytes
import hashlib


@pytest.fixture(params=["local", "remote"])
def execution_lane(request, monkeypatch):
    state = {"run_id": "run", "state": "running", "execution_id": "exec", "execution_epoch": 1}
    repo = ObserverRepository()
    driver = DriverService(repo, FakeCodingAgentProvider())
    failure = AsyncMock()
    repo.get_run = AsyncMock(side_effect=lambda _: SimpleNamespace(**state))
    repo.get_run_status = AsyncMock(side_effect=lambda _: {"run_id": "run", "state": state["state"]})
    repo.fail_run = failure
    monkeypatch.setattr(DriverMCP, "run_view", staticmethod(lambda record: dict(vars(record))))

    async def command(name, arguments, **kwargs):
        if name in {"run.get", "run.status"}:
            return dict(state)
        assert name == "run.fail"
        await failure(arguments["run_id"], arguments["reason"])

    driver.control_client = SimpleNamespace(command=command, driver_epoch=1)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def dispatch(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
            state["state"] = "completed"
            return (SimpleNamespace(**state), None) if request.param == "local" else None
        except asyncio.CancelledError:
            cancelled.set()
            raise

    dispatch_mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(driver, "_dispatch_execution" if request.param == "local" else "_dispatch_remote_execution", dispatch_mock)
    wait = driver._execute_and_wait_local if request.param == "local" else driver._execute_and_wait_remote
    return driver, state, wait, dispatch_mock, failure, entered, release, cancelled


@pytest.mark.asyncio
async def test_concurrent_waiters_share_one_dispatch(execution_lane):
    driver, state, wait, dispatch, failure, entered, release, _ = execution_lane
    first = asyncio.create_task(wait("run", "prompt"))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(wait("run", "prompt"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert [result["state"] for result in results] == ["completed", "completed"]
    assert dispatch.await_count == 1
    assert driver._execution_tasks == {}
    failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_authoritative_cancellation_stops_execution(execution_lane):
    driver, state, wait, _, failure, entered, _, cancelled = execution_lane
    waiter = asyncio.create_task(wait("run", "prompt"))
    await asyncio.wait_for(entered.wait(), 2)
    state["state"] = "cancelled"
    assert (await asyncio.wait_for(waiter, 2))["state"] == "cancelled"
    assert cancelled.is_set()
    assert driver._execution_tasks == {}
    failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_deadline_stops_sandbox_before_settlement(execution_lane):
    driver, state, wait, _, failure, _, _, cancelled = execution_lane
    claim = {"run": dict(state), "request": {"driver_epoch": 1,
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=0.1)).isoformat()}}

    async def fail(*args, **kwargs):
        assert cancelled.is_set()
        driver._execution_loop_stop.set()

    failure.side_effect = fail
    if wait.__name__.endswith("local"):
        driver.control_client = None
        driver.repository.claim_next_execution = AsyncMock(return_value=claim)
    else:
        previous_command = driver.control_client.command

        async def command(name, arguments, **kwargs):
            if name == "execution.claim_next":
                return claim
            return await previous_command(name, arguments, **kwargs)

        driver.control_client.command = command
    await asyncio.wait_for(driver.run_execution_loop(), 3)
    assert driver._execution_tasks == {}
    failure.assert_awaited_once_with("run", {"code": "execution_deadline_exceeded"})


@pytest.mark.asyncio
async def test_cancelling_waiter_preserves_execution_for_next_waiter(execution_lane):
    driver, state, wait, dispatch, failure, entered, release, cancelled = execution_lane
    first = asyncio.create_task(wait("run", "prompt"))
    await asyncio.wait_for(entered.wait(), 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not cancelled.is_set()
    assert len(driver._execution_tasks) == 1
    second = asyncio.create_task(wait("run", "prompt"))
    await asyncio.sleep(0)
    release.set()
    assert (await asyncio.wait_for(second, 2))["state"] == "completed"
    assert dispatch.await_count == 1
    assert driver._execution_tasks == {}
    failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failure_cleans_up_and_preserves_error(execution_lane):
    driver, _, wait, dispatch, failure, *_ = execution_lane
    dispatch.side_effect = RuntimeError("worker_unavailable")
    with pytest.raises(RuntimeError, match="^worker_unavailable$"):
        await wait("run", "prompt")
    failure.assert_awaited_once_with("run", {"code": "worker_unavailable", "message": "worker_unavailable"})
    assert driver._execution_tasks == {}


@pytest.mark.asyncio
async def test_execution_loop_claims_durable_submission_and_settles_outcome():
    repo = ObserverRepository()
    item = await repo.submit_run_plan(
        workspace_id="workspace-default",
        request_id="execution-loop-request",
        conversation_ref="execution-loop-conversation",
        plan=ClosureContract(closure_id="execution-loop-closure", goal="execute", body={"closure_id": "execution-loop-closure"}),
    )

    async def executor(_action: str, payload: dict) -> ExecutionResult:
        value = {"payload": payload}
        digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        return ExecutionResult(
            resource_ref=ResourceRef(resource_id=f"grip://loom.local/workspaces/workspace-default/content/document/sha256/{digest}", identity_criterion="content_digest"),
            value=value,
            replay_safety="Idempotent",
            terminal_state="completed",
        )

    driver = DriverService(
        repo,
        provider=None,
        executor=executor,
        settings=Settings(agent_id="execution-loop-driver", agent_instance_id="execution-loop-instance"),
    )
    driver.start_execution_loop()
    try:
        for _ in range(100):
            current = await repo.get_run(item.run_id)
            if current.state == "completed":
                break
            await asyncio.sleep(0.01)
        current = await repo.get_run(item.run_id)
        assert current.state == "completed"
        queued = await repo.get_execution_request("workspace-default", item.request_id)
        assert queued.state == "settled"
        assert queued.outcome["value"]["payload"] == {}
    finally:
        await driver.stop_execution_loop()


@pytest.mark.asyncio
async def test_execution_slots_are_bounded_and_cancellation_does_not_stop_peers():
    repo = ObserverRepository()
    submissions = [await repo.submit_run_plan(workspace_id="workspace-default", request_id=f"slot-{i}",
        plan=ClosureContract(closure_id=f"slot-{i}", goal="bounded execution", body={"closure_id": f"slot-{i}"}))
        for i in range(3)]
    entered = [asyncio.Event() for _ in submissions]
    releases = [asyncio.Event() for _ in submissions]
    started, cancelled = [], []

    async def executor(_action, payload):
        index = len(started)
        started.append(index)
        entered[index].set()
        try:
            await releases[index].wait()
        except asyncio.CancelledError:
            cancelled.append(index)
            raise
        value = {"index": index}
        digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        return ExecutionResult(resource_ref=ResourceRef(
            resource_id=f"grip://loom.local/workspaces/workspace-default/content/document/sha256/{digest}",
            identity_criterion="content_digest"), value=value, replay_safety="Idempotent", terminal_state="completed")

    driver = DriverService(repo, provider=None, executor=executor,
        settings=Settings(driver_execution_concurrency=2))
    driver.start_execution_loop()
    try:
        await asyncio.wait_for(asyncio.gather(entered[0].wait(), entered[1].wait()), 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(entered[2].wait(), 0.1)
        assert (await repo.get_execution_request("workspace-default", "slot-2")).state == "queued"
        await repo.cancel_run(submissions[0].run_id)
        await asyncio.wait_for(entered[2].wait(), 3)
        assert cancelled == [0]
        assert (await repo.get_run(submissions[1].run_id)).state == "running"
        releases[1].set()
        releases[2].set()
        async with asyncio.timeout(3):
            while True:
                states = [(await repo.get_run(item.run_id)).state for item in submissions]
                if states == ["cancelled", "completed", "completed"]:
                    break
                await asyncio.sleep(0.01)
        assert [len((await repo.get_run(item.run_id)).attempts) for item in submissions] == [1, 1, 1]
    finally:
        await driver.stop_execution_loop()
    assert driver._execution_tasks == {}


@pytest.mark.asyncio
async def test_renewed_lease_releases_a_full_execution_slot(monkeypatch):
    driver = DriverService(ObserverRepository(), None, settings=Settings(driver_execution_concurrency=1))
    entered, recovered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    control = SimpleNamespace(driver_epoch=1)
    claimed_epochs = set()

    async def command(name, _arguments, **_kwargs):
        assert name == "execution.claim_next"
        epoch = control.driver_epoch
        if epoch in claimed_epochs:
            return {}
        claimed_epochs.add(epoch)
        return {"run": {"run_id": "recover-same-run", "state": "running"}, "request": {"driver_epoch": epoch}}

    async def execute(claim):
        if claim["request"]["driver_epoch"] == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        else:
            assert cancelled.is_set()
            recovered.set()
            await asyncio.Event().wait()

    control.command = command
    driver.control_client = control
    monkeypatch.setattr(driver, "_execute_claim", execute)
    driver.start_execution_loop()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        control.driver_epoch = 2
        await asyncio.wait_for(recovered.wait(), 2)
        assert claimed_epochs == {1, 2}
    finally:
        await driver.stop_execution_loop()


@pytest.mark.asyncio
async def test_recovered_execution_is_claimed_by_new_driver_owner():
    repo = ObserverRepository()
    item = await repo.submit_run_plan(
        workspace_id="workspace-default",
        request_id="recovery-request",
        conversation_ref="recovery-conversation",
        plan=ClosureContract(closure_id="recovery-closure", goal="recover", body={"closure_id": "recovery-closure"}),
    )
    first = await repo.claim_next_execution(
        workspace_id="workspace-default", driver_id="driver-a", driver_instance_id="instance-a", driver_epoch=1
    )
    assert first is not None
    await repo.recover_execution_requests_for_driver(
        "workspace-default", driver_id="driver-b", driver_instance_id="instance-b", driver_epoch=2
    )
    second = await repo.claim_next_execution(
        workspace_id="workspace-default", driver_id="driver-b", driver_instance_id="instance-b", driver_epoch=2
    )
    assert second is not None
    assert second["request"]["driver_id"] == "driver-b"
    assert second["request"]["driver_instance_id"] == "instance-b"
    assert second["request"]["driver_epoch"] == 2
