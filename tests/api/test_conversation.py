from tests.support.grip_client import submit_plan, submission
from fastapi.testclient import TestClient

from loom_v2.testing.observer import create_embedded_app


def test_message_and_conversation_routes_are_removed():
    client = TestClient(create_embedded_app())

    assert client.post(
        "/api/v1/messages",
        json={"conversation_ref": "legacy", "text": "hello"},
    ).status_code == 404
    assert client.get("/api/v1/conversations/legacy").status_code == 404
    assert client.post("/api/v1/conversations/legacy/interrupt").status_code == 404


def test_external_submission_is_the_public_task_entrypoint():
    client = TestClient(create_embedded_app())
    response = submit_plan(client, **{
            "idempotency_key": "external-entrypoint",
            "plan": {
                "closure_id": "external-entrypoint-closure",
                "goal": "execute a submitted closure",
                "body": {"closure_id": "external-entrypoint-closure"},
            },
        },
    )

    assert response["kind"] == "reply"
    assert submission(client, "external-entrypoint")["execution_request_state"] == "queued"
