import asyncio
import hashlib

from fastapi.testclient import TestClient

from loom_v2.content_store import canonical_json_bytes
from loom_v2.driver.service import DriverService
from loom_v2.observer.repository import ObserverRepository
from loom_v2.slave.executor import ExecutionResult
from loom_v2.testing.observer import create_embedded_app
from loom_v2.contracts.types import ResourceRef


def _plan(closure_id: str = "submission-closure", goal: str = "run submitted plan") -> dict:
    return {"closure_id": closure_id, "goal": goal, "body": {"closure_id": closure_id}}


def test_external_submission_is_idempotent_and_conflicts_on_changed_payload():
    client = TestClient(create_embedded_app(ObserverRepository()))
    body = {
        "request_id": "external-request-1",
        "conversation_ref": "external-conversation",
        "plan": _plan(),
    }
    first = client.post("/api/v1/run-submissions", json=body)
    second = client.post("/api/v1/run-submissions", json=body)
    assert first.status_code == second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]

    changed = {**body, "plan": _plan(goal="a different plan")}
    conflict = client.post("/api/v1/run-submissions", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "request_id_reused"


def test_cancelled_submission_is_visible_through_submission_query():
    client = TestClient(create_embedded_app(ObserverRepository()))
    submitted = client.post(
        "/api/v1/run-submissions",
        json={"request_id": "external-cancel-1", "conversation_ref": "cancel-conversation", "plan": _plan("cancel-closure")},
    ).json()
    cancelled = client.post(
        f"/api/v1/runs/{submitted['run_id']}/cancel",
        json={"request_id": "external-cancel-1", "reason": "user_cancel"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    queried = client.get("/api/v1/run-submissions/external-cancel-1")
    assert queried.status_code == 200
    assert queried.json()["state"] == "cancelled"


def test_claim_and_driver_settlement_are_projected_to_submission_query():
    repository = ObserverRepository()
    client = TestClient(create_embedded_app(repository))
    submitted = client.post(
        "/api/v1/run-submissions",
        json={"request_id": "external-settle-1", "conversation_ref": "settle-conversation", "plan": _plan("settle-closure")},
    ).json()

    claim = asyncio.run(
        repository.claim_next_execution(
            workspace_id="workspace-default",
            driver_id="test-driver",
            driver_instance_id="test-instance",
            driver_epoch=0,
        )
    )
    assert claim is not None
    assert claim["request"]["request_id"] == "external-settle-1"

    async def execute(_operation: str, payload: dict) -> ExecutionResult:
        value = {"ok": True}
        digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        return ExecutionResult(
            resource_ref=ResourceRef(resource_id=f"content://sha256/{digest}", identity_criterion="content_digest"),
            value=value,
            replay_safety="Idempotent",
            terminal_state="completed",
        )

    driver = DriverService(repository, provider=None, executor=execute)
    completed, _ = asyncio.run(driver._dispatch_execution(submitted["run_id"], ""))
    assert completed.state == "completed"

    queried = client.get("/api/v1/run-submissions/external-settle-1")
    assert queried.status_code == 200
    payload = queried.json()
    assert payload["execution_request_state"] == "settled"
    assert payload["state"] == "completed"
    assert payload["outcome"]["value"] == {"ok": True}
