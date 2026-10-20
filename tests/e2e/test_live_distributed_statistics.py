"""Live end-to-end test for the multi-node distributed-statistics app.

Drives an explicitly configured Observer over the public submission API, then verifies the map-reduce application executed across
both WorkspaceReplica Slaves (slave-a and slave-b) with a correct aggregate.
"""

import json
import os
import math
import time
import importlib.util
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

OBSERVER_URL = os.environ.get("LOOM_LIVE_OBSERVER_URL")
APP_DIR = Path(__file__).resolve().parents[2] / "examples" / "distributed-statistics"
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "accept-bandgap.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("accept_bandgap", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
SCRIPT_MODULE = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(SCRIPT_MODULE)
SlaveFailureController = SCRIPT_MODULE.SlaveFailureController


def test_failure_controller_stops_after_completed_and_running_nodes():
    commands: list[list[str]] = []
    controller = SlaveFailureController(
        docker_bin="docker",
        compose_file="deploy/docker-compose.yml",
        run_command=lambda command: commands.append(command),
    )

    controller.observe(
        {
            "execution_id": "execution-1",
            "execution_epoch": 1,
            "dynamic_nodes": [
                {"node_id": "node-complete", "state": "completed"},
                {"node_id": "node-running", "state": "dispatched"},
            ],
            "attempts": [
                {"node_id": "node-complete", "target": "slave-b", "state": "completed"},
                {"node_id": "node-running", "target": "slave-a", "state": "running"},
            ],
        }
    )
    controller.restore()

    assert commands == [
        ["docker", "compose", "-f", "deploy/docker-compose.yml", "stop", "slave-a"],
        ["docker", "compose", "-f", "deploy/docker-compose.yml", "start", "slave-a"],
    ]
    assert controller.execution_id == "execution-1"
    assert controller.execution_epoch == 1


def test_failure_controller_does_not_stop_before_partial_progress():
    commands: list[list[str]] = []
    controller = SlaveFailureController(
        docker_bin="docker",
        compose_file="deploy/docker-compose.yml",
        run_command=lambda command: commands.append(command),
    )

    controller.observe(
        {
            "dynamic_nodes": [{"node_id": "node-running", "state": "dispatched"}],
            "attempts": [{"node_id": "node-running", "target": "slave-a", "state": "created"}],
        }
    )
    controller.restore()

    assert commands == []


def _read(name: str) -> str:
    return (APP_DIR / name).read_text(encoding="utf-8")


def _put(client, content, media_type: str) -> dict:
    response = client.post("/api/v1/content", json={"content": content, "media_type": media_type})
    response.raise_for_status()
    return response.json()


def _upload_package(client, package_id, descriptor, contract, program, *, orchestration_body=None):
    response = client.post("/api/v1/capability-packages", json={
        "package_id": package_id, "package_version": "v1", "package_type": "function",
        "execution": {"kind": "container:python_orchestrator" if orchestration_body else "process:json_stdio", "version": "1"},
        "action_exports": [{"action_descriptor_ref": descriptor, "io_contract_ref": contract,
                                "effect_class": "Sandboxed", "permissions": [],
                                "replay_safety": "DeterministicByEventLog" if orchestration_body else "DeclaredByPackage"}],
        "body": {"program_content_ref": program, **(orchestration_body or {})},
    })
    response.raise_for_status()
    package = response.json()
    return {"resource_id": package["resource"],
            "version_or_digest": package["package_digest"]}


def _get_run(client: httpx.Client, run_id: str) -> dict:
    response = client.get(f"/api/v1/runs/{run_id}", timeout=30.0)
    response.raise_for_status()
    return response.json()


@pytest.mark.e2e
@pytest.mark.skipif(not OBSERVER_URL, reason="set LOOM_LIVE_OBSERVER_URL to run against a deployed test stack")
def test_live_distributed_statistics_across_both_slaves():
    conversation_ref = f"e2e-distributed-statistics-{uuid4().hex[:10]}"
    with httpx.Client(base_url=OBSERVER_URL) as client:
        health = client.get("/healthz", timeout=10.0).json()
        assert health["ok"] is True

        # 1. Upload JSON Schema documents.
        parent_input_schema = _put(client, json.loads(_read("io/parent.input.schema.json")), "application/schema+json")
        summarize_input_schema = _put(client, json.loads(_read("io/summarize.input.schema.json")), "application/schema+json")
        summarize_output_schema = _put(client, json.loads(_read("io/summarize.output.schema.json")), "application/schema+json")
        merge_input_schema = _put(client, json.loads(_read("io/merge.input.schema.json")), "application/schema+json")
        merge_output_schema = _put(client, json.loads(_read("io/merge.output.schema.json")), "application/schema+json")

        # 2. Upload io.v1 contract documents.
        def _contract(input_ref, output_ref) -> dict:
            return {
                "schema_version": "io.v1",
                "input_schema_ref": input_ref,
                "output_schema_ref": output_ref,
                "success_semantics": None,
                "success_validator_ref": None,
            }

        parent_contract = _put(client, _contract(parent_input_schema, merge_output_schema), "application/vnd.loom.io-contract+json")
        summarize_contract = _put(client, _contract(summarize_input_schema, summarize_output_schema), "application/vnd.loom.io-contract+json")
        merge_contract = _put(client, _contract(merge_input_schema, merge_output_schema), "application/vnd.loom.io-contract+json")

        # 3. Upload capability package programs and the reference-based dataset.
        summarize_program = _put(client, _read("programs/summarize.py"), "text/x-python")
        merge_program = _put(client, _read("programs/merge.py"), "text/x-python")
        partition_refs = [
            _put(client, json.loads(_read(f"data/partitions/p{i}.json")), "application/json")
            for i in range(1, 5)
        ]
        input_ref = _put(client, {"partitions": partition_refs}, "application/json")
        summarize_descriptor = _put(client, {"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/summarize", "name": "summarize"}, "application/vnd.loom.action-descriptor+json")
        merge_descriptor = _put(client, {"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/merge-summaries", "name": "merge-summaries"}, "application/vnd.loom.action-descriptor+json")
        orchestration_descriptor = _put(client, {"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/orchestrate", "name": "orchestrate"}, "application/vnd.loom.action-descriptor+json")

        # Register reusable packages before submitting the complete plan.
        summarize_ref = _upload_package(client, f"summarize-{conversation_ref}", summarize_descriptor, summarize_contract, summarize_program)
        merge_ref = _upload_package(client, f"merge-{conversation_ref}", merge_descriptor, merge_contract, merge_program)

        orchestration_source = (
            _read("programs/orchestrate.py.tpl")
            .replace("SUMMARIZE_REF_PLACEHOLDER", json.dumps(summarize_ref))
            .replace("SUMMARIZE_DESCRIPTOR_REF_PLACEHOLDER", json.dumps(summarize_descriptor))
            .replace("MERGE_REF_PLACEHOLDER", json.dumps(merge_ref))
            .replace("MERGE_DESCRIPTOR_REF_PLACEHOLDER", json.dumps(merge_descriptor))
        )
        orchestration_program = _put(client, orchestration_source, "text/x-python")
        orchestration_ref = _upload_package(
            client, f"orchestrate-{conversation_ref}", orchestration_descriptor, parent_contract, orchestration_program,
            orchestration_body={"allowed_node_package_refs": [summarize_ref, merge_ref], "max_nodes": 6, "max_live_nodes": 2},
        )
        closure = json.loads(_read("closure.json"))
        closure["body"]["program"]["io_contract_ref"] = parent_contract
        closure["body"]["program_systems"] = {
            "execution": {"kind": "container:python_orchestrator", "version": "1"},
            "package_ref": orchestration_ref,
        }
        closure["body"]["node_input_bindings"] = [{"node_id": "grip://loom.local/workspaces/workspace-default/actions/orchestrate", "input_ref": input_ref}]
        submitted = client.post("/api/v1/run-submissions", json={
            "request_id": conversation_ref, "conversation_ref": conversation_ref, "plan": closure,
        })
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["run_id"]

        run = _poll_run(client, run_id, timeout=300.0)
        assert run["state"] == "completed", run.get("outcome")
        assert run["status"] == "completed"

        # 8. Verify multi-node distribution across both Slaves.
        accepted = [event for event in run["events"] if event.get("phase") in {"node_accepted", "node_reassigned"}]
        targets = {
            event.get("selected_target") or event.get("to_target") or event.get("target")
            for event in accepted
        }
        assert {"slave-a", "slave-b"} <= targets, f"expected both slaves, got {targets}"
        assert len(run["dynamic_nodes"]) == 5, len(run["dynamic_nodes"])

        summarize_version_ref = summarize_ref["resource_id"]
        merge_version_ref = merge_ref["resource_id"]
        summarize_nodes = [node for node in run["dynamic_nodes"] if node["package_ref"]["resource_id"] == summarize_version_ref]
        merge_nodes = [node for node in run["dynamic_nodes"] if node["package_ref"]["resource_id"] == merge_version_ref]
        assert len(summarize_nodes) == 4
        assert len(merge_nodes) == 1
        assert all(node["state"] == "completed" for node in run["dynamic_nodes"])

        # 9. Verify the aggregate result is correct and content-addressed.
        resource_ref = run["outcome"]["resource_ref"]
        assert isinstance(resource_ref, dict) and resource_ref["resource_id"].startswith("grip://loom.local/workspaces/workspace-default/content/json-document/sha256/")
        value = run["outcome"]["value"]
        assert value["count"] == 12
        assert value["sum"] == 57
        assert value["min"] == 0
        assert value["max"] == 10
        assert value["mean"] == pytest.approx(57 / 12)
        assert value["stddev"] == pytest.approx(math.sqrt(389 / 12 - (57 / 12) ** 2))

        # Cleanup: close the completed Run.
        client.post(f"/api/v1/runs/{run_id}/close", timeout=30.0)


def _poll_run(client: httpx.Client, run_id: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = _get_run(client, run_id)
        if run["state"] in {"completed", "awaiting_decision", "failed", "cancelled", "closed"}:
            return run
        time.sleep(2.0)
    raise TimeoutError(f"run {run_id} did not reach a terminal state")
