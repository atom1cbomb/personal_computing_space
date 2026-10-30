"""Public GRIP native tasks: discovery, immutable plans and lifecycle actions."""
import asyncio
from copy import deepcopy

from fastapi.testclient import TestClient

from loom_v2.contracts.agents import AgentRegistration
from loom_v2.observer.app import create_app
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from tests.support.grip_client import exchange, invoke, resources


def setup_client():
    settings = Settings(public_base_url="http://testserver", grip_token="grip-test-token")
    repo = ObserverRepository(settings=settings)
    client = TestClient(create_app(repo, settings=settings))
    return repo, client, client.app.state.grip.names


def plan(goal="native task"):
    return {"closure_id": "native", "goal": goal, "body": {"closure_id": "native"}}


def submit(client, n, key="native-1", body=None):
    return invoke(client, n.name("drivers", "default"), n.action("submit-plan"),
                  {"plan": body or plan(), "idempotency_key": key})["run"]


def test_offline_driver_is_discoverable_and_accepts_idempotent_submissions():
    repo, client, n = setup_client()
    driver, = resources(client, "computing/driver")
    assert driver["uri"] == n.name("drivers", "default")
    assert driver["metadata"]["available"] is False
    assert driver["metadata"]["accepting_submissions"] is True
    description = exchange(client, driver["uri"])["body"]
    offers = {o["action"]["uri"]: o for o in description["offers"]}
    schema = invoke(client, offers[n.action("submit-plan")]["action"]["input_schema"], n.action("read"))
    assert {"plan", "idempotency_key"} <= set(schema["required"])
    usage = invoke(client, driver["uri"], n.action("how-to-use"))
    assert "idempotency_key" in usage["usage_markdown"]
    run = submit(client, n)
    assert submit(client, n) == run
    assert len(repo.execution_requests) == 1
    conflict = exchange(client, driver["uri"], action=n.action("submit-plan"),
                        input={"plan": plan("different goal"), "idempotency_key": "native-1"})
    assert conflict["error_code"] == "request_id_reused"
    receipt = invoke(client, driver["uri"], n.action("get-submission"), {"idempotency_key": "native-1"})
    assert receipt["run"] == run
    assert receipt["execution_request_state"] == "queued"
    assert receipt["current_run_state"] == "committed"
    frozen = invoke(client, receipt["plan"], n.action("read"))
    assert frozen["plan"]["goal"] == "native task"
    assert frozen["run"] == run
    assert frozen["snapshot_digest"] == receipt["snapshot_digest"]
    state = invoke(client, run, n.action("read"))
    assert state["plan"] == receipt["plan"]
    assert state["closure_version"] == receipt["closure_version"]
    assert "events" not in state
    detailed = invoke(client, run, n.action("read"), {"include_history": True})
    assert detailed["events"]
    assert resources(client, "execution/plan")[0]["uri"] == receipt["plan"]


def test_cancel_close_and_cross_workspace_are_enforced():
    repo, client, n = setup_client()
    run = submit(client, n)
    assert exchange(client, run, action=n.action("close"), input={})["error_code"] == "run_not_terminal"
    second = submit(client, n, "other")
    assert exchange(client, second, action=n.action("cancel"), input={"idempotency_key": "native-1"})["error_code"] == "request_run_mismatch"
    assert invoke(client, run, n.action("cancel"), {"reason": "user_cancel"})["state"] == "cancelled"
    assert invoke(client, run, n.action("cancel"))["state"] == "cancelled"
    assert invoke(client, run, n.action("close"))["state"] == "closed"
    assert invoke(client, run, n.action("close"))["state"] == "closed"
    foreign = run.replace("workspace-default", "foreign")
    assert exchange(client, foreign)["error_code"] == "not_found"
    assert exchange(client, n.name("drivers", "other"))["error_code"] == "not_found"
    bad_plan = {**plan(), "workspace_id": "foreign"}
    assert exchange(client, n.name("drivers", "default"), action=n.action("submit-plan"),
                    input={"idempotency_key": "foreign", "plan": bad_plan})["error_code"] == "workspace_binding_mismatch"


def test_driver_uri_survives_registration_and_restart():
    repo, client, n = setup_client()
    driver = n.name("drivers", "default")
    for instance in ("first", "second"):
        asyncio.run(repo.register_agent(AgentRegistration(
            role="driver", agent_id="driver-a", instance_id=instance,
            workspace_id="workspace-default", endpoint_url="http://private-driver",
            protocol_version="loom.v1", capabilities={"execution": True, "orchestration": True},
        )))
        current = exchange(client, driver)["body"]["resource"]
        assert current["uri"] == driver
        assert current["metadata"]["available"] is True
        assert current["metadata"]["instance_id"] == instance
        assert "private-driver" not in str(current)


def settle(repo, run_id, terminal_state):
    async def execute():
        claim = await repo.claim_next_execution(workspace_id="workspace-default", driver_id="driver-a",
                                                driver_instance_id="instance-a", driver_epoch=1)
        assert claim["run"]["run_id"] == run_id
        record = await repo.get_run(run_id)
        return await repo.record_result(run_id, {
            "attempt_id": record.attempts[-1]["attempt_id"], "execution_id": record.execution_id,
            "execution_epoch": record.execution_epoch, "terminal_state": terminal_state,
            "value": {"answer": 42}, "terminal_error": {"code": "execution_failed"} if terminal_state == "failed" else None,
        })
    return asyncio.run(execute())


def test_repair_preserves_old_frozen_plan_and_enforces_version_and_success_contract():
    repo, client, n = setup_client()
    original = {**plan(), "required_success_criteria": [{"criterion": "verified answer"}]}
    run = submit(client, n, body=original)
    run_id = run.rsplit("/", 1)[-1]
    receipt = invoke(client, n.name("drivers", "default"), n.action("get-submission"), {"idempotency_key": "native-1"})
    old_plan = invoke(client, receipt["plan"], n.action("read"))
    pending = settle(repo, run_id, "failed")
    assert pending.state == "awaiting_decision"
    repaired = deepcopy(original)
    repaired["body"]["metadata"] = {"repair": "second version"}
    data = {"idempotency_key": "repair-1", "base_closure_version": receipt["closure_version"], "plan": repaired}
    assert exchange(client, run, action=n.action("resubmit"), input={**data, "base_closure_version": "wrong"})["error_code"] == "version_conflict"
    weaker = {**data, "plan": {**repaired, "required_success_criteria": []}}
    assert exchange(client, run, action=n.action("resubmit"), input=weaker)["error_code"] == "success_contract_changed"
    unavailable = deepcopy(data)
    unavailable["plan"]["body"]["compute"] = {"typed_holes": [{"hole_id": "repair-target"}]}
    rejected = invoke(client, run, n.action("resubmit"), unavailable)
    assert rejected["rejected"]["details"]["blockers"][0]["code"] == "typed_hole_unbound"
    assert invoke(client, run, n.action("read"))["plan"] == receipt["plan"]
    assert invoke(client, run, n.action("resubmit"), data) == {"run": run}
    assert invoke(client, run, n.action("resubmit"), data) == {"run": run}
    current = invoke(client, run, n.action("read"))
    assert current["plan"] != receipt["plan"]
    assert invoke(client, receipt["plan"], n.action("read")) == old_plan
    assert invoke(client, current["plan"], n.action("read"))["plan"]["body"]["metadata"] == {"repair": "second version"}
    assert len(resources(client, "execution/plan")) == 2


def test_attestation_accept_and_repeated_resolution():
    repo, client, n = setup_client()
    run = submit(client, n)
    pending = settle(repo, run.rsplit("/", 1)[-1], "decision_required")
    assert pending.outcome["decision"] == "attestation"
    assert invoke(client, run, n.action("resolve"), {"decision": "accept"})["state"] == "completed"
    assert invoke(client, run, n.action("resolve"), {"decision": "accept"})["state"] == "completed"
    assert invoke(client, run, n.action("close"))["state"] == "closed"


async def test_native_reads_refresh_submission_and_history_from_durable_state(tmp_path):
    import httpx
    from sqlalchemy.ext.asyncio import create_async_engine
    from loom_orchestration import LoomClient
    from loom_v2.contracts.types import ClosureContract

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native.db'}")
    settings = Settings(public_base_url="http://observer", grip_token="grip-test-token")
    writer = ObserverRepository(engine, settings=settings)
    await writer.init_db()
    reader = ObserverRepository(engine, settings=settings)
    item = await writer.submit_run_plan(workspace_id=settings.workspace_id, request_id="durable", plan=ClosureContract.model_validate(plan()))
    app = create_app(reader, settings=settings)
    async with LoomClient("http://observer", token=settings.grip_token, allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=app)) as client:
        driver = client.names.name("drivers", "default")
        run = client.names.name("runs", item.run_id)
        assert (await client.invoke(driver, client.names.action("get-submission"), {"idempotency_key": "durable"}))["state"] == "committed"
        assert (await client.invoke(run, client.names.action("read"), {"include_history": True}))["state"] == "committed"
        await writer.cancel_run(item.run_id, "cancel from another Observer")
        receipt = await client.invoke(driver, client.names.action("get-submission"), {"idempotency_key": "durable"})
        assert receipt["state"] == "cancelled"
        assert receipt["execution_request_state"] == "settled"
        assert (await client.invoke(run, client.names.action("read"), {"include_history": True}))["state"] == "cancelled"
    await engine.dispose()


def test_committed_run_without_submission_does_not_advertise_a_missing_plan():
    repo, client, n = setup_client()

    async def commit_only():
        record = await repo.open_run("unqueued", "", "unsubmitted plan")
        await repo.begin_refinement(record.run_id)
        await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    asyncio.run(commit_only())
    run = n.name("runs", "unqueued")
    described = exchange(client, run)["body"]["resource"]
    assert described["metadata"]["closure_version"]
    assert described["metadata"]["plan"] is None
    assert invoke(client, run, n.action("read"))["plan"] is None
    assert invoke(client, run, n.action("read"), {"include_history": True})["plan"] is None
    assert resources(client, "execution/plan") == []


async def test_sdk_selects_default_driver_and_preserves_readiness_diagnostics():
    import httpx
    import pytest
    from loom_orchestration import ClosureContract, LoomClient, PlanRejectedError
    from jsonschema import Draft202012Validator

    repo, http, n = setup_client()
    async with LoomClient("http://testserver", token="grip-test-token", allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=http.app)) as client:
        await client.invoke(n.root, n.action("register-resource"), {
            "resource_id": n.name("resources", "driver-description"), "mcue_ref": n.mcue("computing/driver"),
        })
        assert len(await client.resources(mcue="computing/driver")) == 2
        blocked = ClosureContract.model_validate({**plan(), "body": {"compute": {"typed_holes": [{"hole_id": "missing-worker"}]}}})
        driver = n.name("drivers", "default")
        raw = await client.invoke(driver, n.action("submit-plan"), {"idempotency_key": "blocked", "plan": blocked.model_dump(mode="json")})
        assert raw["rejected"]["details"]["blockers"] == [{"code": "typed_hole_unbound", "hole_id": "missing-worker"}]
        description = await client.describe(driver)
        offer = next(o for o in description["offers"] if o["action"]["uri"] == n.action("submit-plan"))
        schema = await client.invoke(offer["action"]["output_schema"], n.action("read"), {})
        Draft202012Validator(schema).validate(raw)
        with pytest.raises(PlanRejectedError) as caught:
            await client.submit(request_id="blocked", plan=blocked)
        assert caught.value.code == "readiness_blocked"
        assert caught.value.details["blockers"][0]["hole_id"] == "missing-worker"
        assert not repo.execution_requests
        accepted = await client.submit(request_id="blocked", plan=ClosureContract.model_validate(plan()))
        assert accepted["execution_request_state"] == "queued"


def test_submit_plan_requires_owner_authentication():
    repo, client, n = setup_client()
    response = exchange(client, n.name("drivers", "default"), action=n.action("submit-plan"),
                        input={"idempotency_key": "unauthorized", "plan": plan()},
                        auth={"scheme": "bearer", "credential": "invalid-owner-token"})
    assert response["kind"] == "error"
    assert not repo.execution_requests


def test_native_submission_recovers_after_driver_takeover_without_changing_run_or_plan():
    import pytest
    from loom_v2.contracts.agents import DriverCommand

    repo, client, n = setup_client()
    run_uri = submit(client, n)
    frozen_uri = invoke(client, run_uri, n.action("read"))["plan"]
    frozen = invoke(client, frozen_uri, n.action("read"))

    async def takeover():
        async def register(instance):
            return await repo.register_agent(AgentRegistration(
                role="driver", agent_id="driver-a", instance_id=instance,
                workspace_id="workspace-default", endpoint_url="http://driver",
                protocol_version="loom.v1", capabilities={"execution": True, "orchestration": True},
            ))

        async def command(lease, name, key):
            return await repo.execute_driver_command(DriverCommand(
                request_id=key, driver_id=lease.agent_id, instance_id=lease.instance_id,
                lease_id=lease.lease_id, driver_epoch=lease.epoch, command=name,
                arguments={"workspace_id": "workspace-default"},
            ))

        old = await register("old")
        first = await command(old, "execution.claim_next", "first-claim")
        current = await register("new")
        with pytest.raises(ValueError, match="stale_driver_epoch"):
            await command(old, "execution.claim_next", "stale-claim")
        await command(current, "execution.recover", "recover")
        resumed = await command(current, "execution.claim_next", "resumed-claim")
        assert resumed["run"]["run_id"] == first["run"]["run_id"]
        assert resumed["request"]["execution_id"] == first["request"]["execution_id"]
        assert resumed["request"]["driver_epoch"] == current.epoch

    asyncio.run(takeover())
    assert submit(client, n) == run_uri
    assert len(repo.execution_requests) == 1
    assert invoke(client, run_uri, n.action("read"))["plan"] == frozen_uri
    assert invoke(client, frozen_uri, n.action("read")) == frozen
