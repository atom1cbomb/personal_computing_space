import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from loom_v2.contracts.types import ClosureContract, TaskClosure
from loom_v2.content_store import canonical_json_bytes
from loom_v2.testing.observer import create_embedded_app as create_app
from loom_v2.observer.repository import ObserverRepository


def test_complete_submission_is_queued_and_queryable():
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/run-submissions",
        json={
            "request_id": "request-1",
            "conversation_ref": "task-1",
            "plan": {"closure_id": "closure-1", "goal": "validate result", "body": {"closure_id": "closure-1"}},
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["execution_request_state"] == "queued"
    assert payload["current_run_state"] == "committed"
    assert client.get("/api/v1/run-submissions/request-1").json()["run_id"] == payload["run_id"]


def test_submission_persists_complete_closure_contract():
    client = TestClient(create_app())
    contract = {
        "closure_id": "closure-transform",
        "goal": "transform numbers",
        "required_success_criteria": [{"criterion_id": "ordered", "kind": "deterministic"}],
        "allowed_effects": ["read_workspace"],
        "resource_budget": {"max_node_concurrency": 1, "max_attempts": 2},
        "recovery_policy": {"allow_reassignment": True, "retry_on": ["timeout"]},
        "result_expectations": [{"kind": "content", "identity_criterion": "content_digest"}],
        "declared_constraints": [],
        "body": {
            "closure_id": "closure-transform",
        },
    }
    response = client.post(
        "/api/v1/run-submissions",
        json={"request_id": "conversation-contract-request", "conversation_ref": "conversation-contract", "plan": contract},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["closure_contract"]["goal"] == "transform numbers"
    assert payload["closure_contract"]["required_success_criteria"][0]["criterion_id"] == "ordered"
    assert payload["closure_contract"]["resource_budget"]["max_attempts"] == 2


def test_duplicate_submission_returns_same_run():
    client = TestClient(create_app())
    body = {
        "request_id": "same-request",
        "conversation_ref": "task-2",
        "plan": {"closure_id": "closure-2", "goal": "validate result", "body": {"closure_id": "closure-2"}},
    }
    first = client.post("/api/v1/run-submissions", json=body)
    second = client.post("/api/v1/run-submissions", json=body)
    assert first.status_code == second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]


@pytest.mark.asyncio
async def test_observer_record_result_projects_schema_failure_to_awaiting_decision():
    repo = ObserverRepository()
    schema_ref = await repo.put_content({"type": "object", "required": ["expected"]}, media_type="application/schema+json")
    contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": None,
            "output_schema_ref": schema_ref.model_dump(mode="json"),
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    contract = ClosureContract(
        closure_id="closure-record-validation",
        goal="validate output",
        body=TaskClosure(
            closure_id="closure-record-validation",
            program={"io_contract_ref": contract_ref.model_dump(mode="json")},
        ),
    )
    record = await repo.open_run("run-record-validation", "conversation-record-validation", "validate output", closure_contract=contract)
    await repo.begin_refinement(record.run_id)
    committed = await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    await repo.start(record.run_id, committed.version_id)
    attempt_id = (await repo.get_run(record.run_id)).attempts[0]["attempt_id"]
    value = {"wrong": True}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    updated = await repo.record_result(
        record.run_id,
        {
            "attempt_id": attempt_id,
            "execution_id": record.execution_id,
            "execution_epoch": 1,
            "resource_ref": {"resource_id": f"grip://loom.local/workspaces/workspace-default/content/execution-result/sha256/{digest}", "identity_criterion": "content_digest"},
            "value": value,
        },
    )

    assert updated.state == "awaiting_decision"
    assert updated.outcome["error"]["code"] == "output_schema_mismatch"
    assert updated.outcome["resource_ref"] is None
    assert any(event["phase"] == "io_schema_rejected" for event in updated.events)


@pytest.mark.asyncio
async def test_observer_recomputes_output_digest_and_resource_ref_identity():
    repo = ObserverRepository()
    record = await repo.open_run("run-digest-validation", "conversation-digest-validation", "validate digest")
    await repo.begin_refinement(record.run_id)
    committed = await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    await repo.start(record.run_id, committed.version_id)
    attempt_id = (await repo.get_run(record.run_id)).attempts[0]["attempt_id"]

    with pytest.raises(ValueError, match="resource_ref_digest_mismatch"):
        await repo.record_result(
            record.run_id,
            {
                "attempt_id": attempt_id,
                "execution_id": record.execution_id,
                "execution_epoch": 1,
                "resource_ref": {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/result-forged", "version_or_digest": "f" * 64},
                "value": {"text": "hello"},
            },
        )


@pytest.mark.asyncio
async def test_observer_requires_execution_id_on_terminal_report():
    repo = ObserverRepository()
    record = await repo.open_run("run-execution-id", "conversation-execution-id", "validate execution id")
    await repo.begin_refinement(record.run_id)
    committed = await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    await repo.start(record.run_id, committed.version_id)
    attempt_id = (await repo.get_run(record.run_id)).attempts[0]["attempt_id"]

    with pytest.raises(ValueError, match="stale_execution_id"):
        await repo.record_result(
            record.run_id,
            {"attempt_id": attempt_id, "execution_epoch": 1, "value": {"text": "hello"}},
        )


@pytest.mark.asyncio
async def test_observer_rejects_stale_attempt_and_epoch_terminal_reports():
    repo = ObserverRepository()
    record = await repo.open_run("run-stale-terminal", "conversation-stale-terminal", "validate terminal fencing")
    await repo.begin_refinement(record.run_id)
    committed = await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    await repo.start(record.run_id, committed.version_id)
    attempt_id = (await repo.get_run(record.run_id)).attempts[0]["attempt_id"]

    with pytest.raises(ValueError, match="stale_attempt"):
        await repo.record_result(
            record.run_id,
            {"attempt_id": "other-attempt", "execution_id": record.execution_id, "execution_epoch": 1, "value": {"text": "hello"}},
        )
    with pytest.raises(ValueError, match="stale_execution_epoch"):
        await repo.record_result(
            record.run_id,
            {"attempt_id": attempt_id, "execution_id": record.execution_id, "execution_epoch": 2, "value": {"text": "hello"}},
        )


@pytest.mark.asyncio
async def test_observer_reexecutes_validator_instead_of_trusting_slave_evidence():
    repo = ObserverRepository()
    validator_ref = await repo.put_content(
        b'import json; print(json.dumps({"result": "fail", "errors": [{"path": "$", "keyword": "criterion", "message": "not sorted"}]}))',
        media_type="text/x-python",
    )
    contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": None,
            "output_schema_ref": None,
            "success_semantics": {"criterion": "sorted"},
            "success_validator_ref": validator_ref.model_dump(mode="json"),
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    contract = ClosureContract(
        closure_id="closure-validator-authority",
        goal="validate result",
        body=TaskClosure(
            closure_id="closure-validator-authority",
            program={"io_contract_ref": contract_ref.model_dump(mode="json")},
        ),
    )
    record = await repo.open_run("run-validator-authority", "conversation-validator-authority", "validate result", closure_contract=contract)
    await repo.begin_refinement(record.run_id)
    committed = await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    await repo.start(record.run_id, committed.version_id)
    current = await repo.get_run(record.run_id)
    attempt_id = current.attempts[0]["attempt_id"]
    value = {"text": "hello"}
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    updated = await repo.record_result(
        record.run_id,
        {
            "attempt_id": attempt_id,
            "execution_id": current.execution_id,
            "execution_epoch": current.execution_epoch,
            "resource_ref": {"resource_id": f"grip://loom.local/workspaces/workspace-default/content/execution-result/sha256/{digest}", "identity_criterion": "content_digest"},
            "value": value,
            "validation_evidence": [
                {
                    "evidence_id": "slave-forged-pass",
                    "attempt_id": attempt_id,
                    "execution_epoch": current.execution_epoch,
                    "validator_ref": validator_ref.model_dump(mode="json"),
                    "result": "pass",
                    "issuer": "slave",
                    "created_at": "2026-08-29T00:00:00+00:00",
                }
            ],
        },
    )

    assert updated.state == "awaiting_decision"
    assert updated.outcome["terminal_error"]["code"] == "success_validation_failed"
    assert any(item.get("issuer") == "observer" and item.get("result") == "fail" for item in updated.outcome["validation_evidence"])
