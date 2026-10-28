import asyncio
import json
from pathlib import Path

import pytest

from loom_orchestration import CallLedger, inventory, publish_function, submit_map_reduce, wait_run, read_package_source
from loom_orchestration.service_package import ServicePackage
from tests.api.test_grip import setup


def test_budget_survives_restart_counts_failed_attempts_and_rejects_reused_key(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = CallLedger(path, max_calls=3, max_text_characters=15, max_text_length=10)
    identity, fresh, _ = ledger.reserve("target", "embed", {"texts": ["abc", "abc"]}, "first")
    assert fresh
    ledger.record(identity, {"vectors": [[1], [1]]})
    restarted = CallLedger(path, max_calls=3, max_text_characters=15, max_text_length=10)
    assert restarted.reserve("target", "embed", {"texts": ["abc", "abc"]}, "first")[1:] == (False, {"vectors": [[1], [1]]})
    restarted.reserve("target", "embed", {"texts": ["1234567"]}, "uncertain")
    assert restarted.summary()["text_characters"] == 13
    with pytest.raises(ValueError, match="call_budget_exceeded"):
        restarted.reserve("target", "embed", {"texts": ["abc"]}, "over")
    with pytest.raises(ValueError, match="per_text_budget_exceeded"):
        restarted.reserve("target", "embed", {"texts": ["x" * 11]}, "long")
    with pytest.raises(ValueError, match="different_arguments"):
        restarted.reserve("target", "embed", {"texts": ["abcd"]}, "first")
    with pytest.raises(ValueError, match="call_budget_changed"):
        CallLedger(path, max_calls=100)


async def test_inventory_retains_ready_and_auth_blocked_service_categories():
    class Client:
        async def resources(self):
            return [
                {"uri": "kb", "mcue": "capability/activation", "metadata": {"state": "ready", "actions": [{"uri": "read", "name": "documents_info"}]}},
                {"uri": "vm", "mcue": "capability/activation", "metadata": {"state": "pending_auth"}},
                {"uri": "old", "mcue": "capability/activation", "metadata": {"state": "stopped"}},
                {"uri": "history", "mcue": "execution/run", "metadata": {}},
                {"uri": "physical", "mcue": "computing/vm", "metadata": {"attributes": {"cloud_id": "instance"}}},
            ]
    result = await inventory(Client())
    assert [r["uri"] for r in result["activations"]] == ["kb", "vm"]
    assert result["resources"][0]["attributes"]["cloud_id"] == "instance"


async def test_wait_run_default_follows_server_terminal_state():
    class Client:
        def __init__(self):
            self.polls = 0

        async def run(self, _run_id):
            self.polls += 1
            return {"state": "running" if self.polls == 1 else "failed"}

    client = Client()
    outcome = await wait_run(client, "grip://loom.local/runs/example", interval=0.001)
    assert outcome["status"] == "failed"
    assert client.polls == 2


async def test_map_reduce_executes_three_nodes_and_returns_persisted_reducer_result(setup):
    app, repo, client, _ = setup
    async def check_response(response):
        if response.status_code >= 400:
            await response.aread()
            pytest.fail(response.text)
    client._client.event_hooks["response"] = [check_response]
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    worker = await publish_function(client, package_id="wf-worker", package_version="v1", action_name="double",
        source='import json,sys;d=json.load(sys.stdin);print(json.dumps({"value":2*d["value"]}))',
        input_schema=schema, output_schema=schema)
    reducer = await publish_function(client, package_id="wf-reducer", package_version="v1", action_name="sum",
        source='import json,sys;d=json.load(sys.stdin);print(json.dumps({"value":sum(x["value"] for x in d["inputs"])}))',
        input_schema={"type": "object", "properties": {"inputs": {"type": "array", "items": schema}}, "required": ["inputs"]}, output_schema=schema)
    slaves = [r["uri"] for r in await client.resources(mcue="computing/slave") if r["metadata"].get("available")]
    app.state.driver.start_execution_loop()
    result = await submit_map_reduce(client, worker_package=worker["resource"], worker_action="double",
        reducer_package=reducer["resource"], reducer_action="sum",
        jobs=[{"target": slaves[0], "arguments": {"value": 17}}, {"target": slaves[-1], "arguments": {"value": 23}}],
        reducer_target=slaves[0], package_id="wf-orchestrator", request_id="wf-map-reduce", goal="Sum two remote doubles")
    outcome = await wait_run(client, result["receipt"]["run_id"], timeout=30, interval=.02)
    assert outcome["status"] == "completed", json.dumps(outcome["run"].get("outcome"), indent=2)
    assert outcome["value"] == {"value": 80}
    assert outcome["digest_verified"]
    source = await read_package_source(client, worker["resource"])
    assert '2*d["value"]' in source["source"]


async def test_service_source_is_opt_in_and_bound_to_image(setup, tmp_path):
    app, repo, client, _ = setup
    directory = Path(__file__).resolve().parents[2] / "capability_services/outline_http"
    package = ServicePackage(directory)
    image = "registry.example/outline@sha256:" + "d" * 64
    uri = await package.publish(client, image_ref=image, source_files=["server.py", "Dockerfile"])
    source = await read_package_source(client, uri)
    assert source["image_ref"] == image
    assert set(source["files"]) == {"server.py", "Dockerfile"}
    assert source["files"]["server.py"] == (directory / "server.py").read_text()
    with pytest.raises(ValueError, match="source_path_invalid"):
        await package.publish(client, image_ref=image, source_files=["../README.md"])
