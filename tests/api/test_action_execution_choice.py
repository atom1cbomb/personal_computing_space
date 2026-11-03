"""Direct execution and explicit Run submission share one action contract."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time

import httpx
import pytest

from loom_orchestration import CallLedger, GripError, LoomClient, execute_action, inventory, publish_function, wait_run
from loom_orchestration.grip import Request
from loom_v2.slave.grip_data import create_data_app
from tests.api.test_grip import setup, wait_result
from tests.api.test_grip_service_bindings_api import service_setup
from tests.support.action_plan import submit_activation_plan
from tests.support.asgi_server import running_asgi
from tests.support.package_fixture import process_package_value


async def test_same_function_direct_and_documented_plan_persist_only_task(setup, monkeypatch, tmp_path):
    app, repo, client, _ = setup
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    published = await publish_function(client, package_id="both", package_version="v1", action_name="double",
        source='import json,sys;d=json.load(sys.stdin);print(json.dumps({"value":2*d["value"]}))',
        input_schema=schema, output_schema=schema)
    activated = await client.invoke(client.names.name("slaves", "slave-a"), client.names.action("activate-package"),
                                    {"package": published["resource"], "instance_name": "chosen", "idempotency_key": "activate"})
    target = activated["resource"]
    action, = (await client.describe(target))["resource"]["metadata"]["actions"]
    assert action["execution"] == {m: {"available": True, "unavailable_reason": None} for m in ("direct", "task")}
    writes = []
    original_put = repo.content_store.put
    async def put(*args, **kwargs):
        writes.append(True)
        return await original_put(*args, **kwargs)
    monkeypatch.setattr(repo.content_store, "put", put)
    direct = await execute_action(client, target, action["uri"], {"value": 21}, idempotency_key="explore")
    assert direct["value"] == {"value": 42}
    assert not writes and not repo.runs and not app.state.slaves["slave-a"].attempts
    listed = next(a for a in (await inventory(client))["activations"] if a["uri"] == target)
    assert listed["actions"][0]["execution"] == action["execution"]

    reference = Path(__file__).parents[2] / "skills/personal-computing-space/references/single-action-plan.md"
    code, = re.findall(r"```python\n(.*?)```", reference.read_text(), re.S)
    namespace = {}
    exec('async def submit_example(client, activation_uri, action_uri, arguments, key):\n' +
         ''.join('    ' + line + '\n' for line in code.splitlines()) + '    return submission\n', namespace)
    submission = await namespace["submit_example"](client, target, action["uri"], {"value": 21}, "durable")
    outcome = await wait_result(app, client, submission["run"])
    assert outcome["state"] == "completed" and outcome["outcome"]["value"] == direct["value"]
    assert writes and app.state.slaves["slave-a"].attempts

    # Loss of a public JSON route leaves the same action usable as a task node.
    app.state.service_access.routes.embedded = lambda _: False
    description = await client.describe(target, refresh=True)
    action, = description["resource"]["metadata"]["actions"]
    assert action["available"] and action["execution"]["task"]["available"]
    assert action["execution"]["direct"] == {"available": False, "unavailable_reason": "binding_unavailable"}
    assert action["uri"] not in {o["action"]["uri"] for o in description["offers"]}
    with pytest.raises(ValueError, match="direct_execution_unavailable"):
        await execute_action(client, target, action["uri"], {"value": 1}, idempotency_key="no-route")
    second = await submit_activation_plan(client, target, action["uri"], {"value": 2}, "no-route-task")
    assert (await wait_result(app, client, second["run"]))["outcome"]["value"] == {"value": 4}


@pytest.mark.parametrize("failure", ["schema", "validator"])
async def test_function_output_validation_is_shared_by_direct_and_task(setup, failure):
    app, repo, client, _ = setup
    schema = await repo.put_content({"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}, media_type="application/schema+json")
    validator = await repo.put_content('print(\'{"result":"fail"}\')', media_type="text/x-python")
    contract = await repo.put_content({"schema_version": "io.v1", "input_schema_ref": schema.model_dump(mode="json"),
        "output_schema_ref": schema.model_dump(mode="json"),
        "success_validator_ref": validator.model_dump(mode="json") if failure == "validator" else None}, media_type="application/json")
    source = 'print(\'{"value":"bad"}\')' if failure == "schema" else 'print(\'{"value":1}\')'
    program = await repo.put_content(source, media_type="text/x-python")
    package = await client.register_package(process_package_value(repo, package_id="invalid-result", action_ref=client.names.action("validate"),
        program_content_ref=program, io_contract_ref=contract))
    activated = await client.invoke(client.names.name("slaves", "slave-a"), client.names.action("activate-package"),
        {"package": package["resource"], "idempotency_key": "prepare"})
    target = activated["resource"]
    action, = (await client.describe(target))["resource"]["metadata"]["actions"]
    with pytest.raises(GripError, match="internal_error"):
        await client.invoke(target, action["uri"], {"value": 1})
    assert not repo.runs and not app.state.slaves["slave-a"].attempts
    submission = await submit_activation_plan(client, target, action["uri"], {"value": 1}, "invalid-result")
    app.state.driver.start_execution_loop()
    outcome = (await wait_run(client, submission["run"], timeout=5, interval=.01))["run"]
    assert outcome["state"] in {"failed", "awaiting_decision"}, outcome
    assert outcome["outcome"]["terminal_error"]["code"] == ("output_schema_mismatch" if failure == "schema" else "success_validation_failed")


async def test_json_service_can_also_run_as_task(service_setup):
    app, repo, slave, package, target, actions, calls = service_setup
    from runtime_plugins.container_http_v1.runtime import RUNTIME_DESCRIPTOR
    agent = next(a for a in repo.agents.values() if a["role"] == "slave" and a["agent_id"] == "slave-a")
    agent["capabilities"]["runtime_plugin_descriptors"] = [RUNTIME_DESCRIPTOR]
    await repo.refresh_slaves(repo.settings.workspace_id)
    async with LoomClient("http://observer", token="owner", transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
        direct = await client.invoke(target, actions[0], {"value": 7})
        assert not repo.runs and not slave.attempts
        submission = await submit_activation_plan(client, target, actions[0], {"value": 7}, "service-task")
        try:
            outcome = await wait_result(app, client, submission["run"])
            assert outcome["state"] == "completed", outcome
            assert outcome["outcome"]["value"] == direct
            assert calls == [{"value": 7}, {"value": 7}]
        finally:
            await app.state.driver.stop_execution_loop()


async def test_direct_and_relay_can_both_wait_more_than_thirty_seconds(service_setup):
    app, repo, slave, _, target, actions, _ = service_setup
    budgets = []
    async def slow(package, export, value, **kwargs):
        budgets.append(kwargs["deadline_seconds"])
        await asyncio.sleep(30.1)
        return {"value": value}
    slave.runtime_plugin_host.invoke = slow
    async with running_asgi(create_data_app(slave)) as data_base:
        route = {"protocol": "loom-http-json.v1", "audience": slave.grip_data.audience,
                 "endpoint": data_base + "/.well-known/grip"}
        app.state.service_access.settings.grip_data_bindings = {slave.grip_data.audience: [route]}
        async with running_asgi(app) as base:
            from loom_v2.observer.grip import GripService
            app.state.service_access.settings.public_base_url = base
            app.state.grip = GripService(app, app.state.service_access.settings)
            app.state.slaves = {}
            async def call(endpoint):
                async with LoomClient(base, token="owner", timeout=None, connect_timeout=3, allow_insecure_grip=True,
                    trusted_bindings=[route], binding_preference=[endpoint]) as client:
                    return await client.invoke(target, actions[0], {"value": 8})
            try:
                results = await asyncio.gather(call(route["endpoint"]), call(base + "/.well-known/grip"))
                assert results == [{"value": 8}, {"value": 8}]
                assert len(budgets) == 2 and all(b > 30 for b in budgets)
                assert not repo.runs and not slave.attempts
            finally:
                app.state.slaves = {"slave-a": slave}


@pytest.mark.parametrize("limit", ["deadline", "grant"])
async def test_direct_limits_cancel_runtime_and_clear_admission(service_setup, limit):
    app, repo, slave, _, target, actions, _ = service_setup
    cancelled = asyncio.Event()
    async def block(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    slave.runtime_plugin_host.invoke = block
    async with LoomClient("http://observer", token="owner", transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
        _, payload, _, _ = await client._prepare(target, actions[0], {"value": 1}, mode="request-reply")
        request = Request.model_validate(payload)
        claims = app.state.service_access.tokens.verify(request, client.names.root)
        if limit == "deadline":
            request = request.model_copy(update={"deadline": (datetime.now(timezone.utc) + timedelta(seconds=.1)).isoformat()})
        else:
            claims["exp"] = time.time() + .1
        with pytest.raises(TimeoutError):
            await slave.grip_data.invoke(request, claims=claims)
        assert cancelled.is_set() and not slave.grip_data.calls
        assert not repo.runs and not slave.attempts


async def test_direct_transport_failure_never_submits_or_replays(setup, monkeypatch, tmp_path):
    app, repo, client, package = setup
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    package = await publish_function(client, package_id="uncertain", package_version="v1", action_name="call",
        source='print(\'{"value":1}\')', input_schema=schema, output_schema=schema)
    active = await client.invoke(client.names.name("slaves", "slave-a"), client.names.action("activate-package"),
        {"package": package["resource"], "idempotency_key": "active"})
    count = []
    original_invoke = client.invoke
    async def unavailable(target, *args, **kwargs):
        if target != active["resource"]:
            return await original_invoke(target, *args, **kwargs)
        count.append(True)
        raise httpx.ReadError("connection lost")
    monkeypatch.setattr(client, "invoke", unavailable)
    ledger = CallLedger(tmp_path / "ledger.db", max_calls=2)
    with pytest.raises(httpx.ReadError):
        await execute_action(client, active["resource"], "call", {"value": 1}, idempotency_key="once", ledger=ledger)
    with pytest.raises(ValueError, match="prior_direct_call_outcome_unknown"):
        await execute_action(client, active["resource"], "call", {"value": 1}, idempotency_key="once", ledger=ledger)
    assert count == [True] and not repo.runs


async def test_non_object_result_remains_task_only(setup):
    app, repo, client, _ = setup
    package = await publish_function(client, package_id="scalar", package_version="v1", action_name="count",
        source='print("5")', input_schema={"type": "object"}, output_schema={"type": "integer"})
    activated = await client.invoke(client.names.name("slaves", "slave-a"), client.names.action("activate-package"),
        {"package": package["resource"], "idempotency_key": "scalar"})
    description = await client.describe(activated["resource"])
    action, = description["resource"]["metadata"]["actions"]
    assert action["execution"] == {"task": {"available": True, "unavailable_reason": None}}
    assert action["uri"] not in {o["action"]["uri"] for o in description["offers"]}
    submission = await submit_activation_plan(client, activated["resource"], action["uri"], {}, "scalar")
    outcome = await wait_result(app, client, submission["run"])
    assert outcome["state"] == "completed" and outcome["outcome"]["value"] == 5
