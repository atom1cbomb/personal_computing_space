import asyncio

from fastapi.testclient import TestClient

from loom_v2.observer.repository import ObserverRepository
from loom_v2.testing.observer import create_embedded_app
from tests.support.grip_client import run_action, submit_plan, submission


def _plan(closure_id="submission-closure", goal="run submitted plan"):
    return {"closure_id": closure_id, "goal": goal, "body": {"closure_id": closure_id}}


def test_external_submission_is_idempotent_and_conflicts_on_changed_payload():
    client = TestClient(create_embedded_app(ObserverRepository()))
    body = {"idempotency_key": "external-request-1", "conversation_ref": "external-conversation", "plan": _plan()}
    first = submit_plan(client, **body)
    second = submit_plan(client, **body)
    assert first["kind"] == second["kind"] == "reply"
    assert second["body"] == first["body"]
    conflict = submit_plan(client, **{**body, "plan": _plan(goal="a different goal")})
    assert conflict["error_code"] == "request_id_reused"


def test_external_submission_can_be_cancelled_before_driver_claims_it():
    repo = ObserverRepository()
    client = TestClient(create_embedded_app(repo))
    assert submit_plan(client, idempotency_key="external-cancel-1", plan=_plan())["kind"] == "reply"
    submitted = submission(client, "external-cancel-1")
    cancelled = run_action(client, submitted["run_id"], "cancel", {"idempotency_key": "external-cancel-1", "reason": "user_cancel"})
    assert cancelled["state"] == "cancelled"
    queried = submission(client, "external-cancel-1")
    assert queried["execution_request_state"] == "settled"
    assert queried["state"] == "cancelled"


def test_execution_request_settles_when_run_completes():
    repo = ObserverRepository()
    client = TestClient(create_embedded_app(repo))
    assert submit_plan(client, idempotency_key="external-settle-1", plan=_plan())["kind"] == "reply"
    submitted = submission(client, "external-settle-1")

    async def execute():
        claim = await repo.claim_next_execution(workspace_id="workspace-default", driver_id="driver-a", driver_instance_id="instance-a", driver_epoch=1)
        assert claim["run"]["run_id"] == submitted["run_id"]
        run = await repo.get_run(submitted["run_id"])
        return await repo.record_result(run.run_id, {
            "attempt_id": run.attempts[-1]["attempt_id"], "execution_id": run.execution_id,
            "execution_epoch": run.execution_epoch, "terminal_state": "completed", "value": {"ok": True},
        })

    assert asyncio.run(execute()).state == "completed"
    payload = submission(client, "external-settle-1")
    assert payload["execution_request_state"] == "settled"
    assert payload["state"] == "completed"
    assert payload["outcome"]["value"] == {"ok": True}


def test_replaced_task_rest_routes_are_removed():
    client = TestClient(create_embedded_app())
    assert client.post("/api/v1/run-submissions", json={}).status_code == 404
    assert client.get("/api/v1/run-submissions/key").status_code == 404
    assert client.get("/api/v1/runs/example").status_code == 404
    for action in ("cancel", "resubmit", "resolve", "close"):
        assert client.post(f"/api/v1/runs/example/{action}", json={}).status_code == 404
