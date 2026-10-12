from fastapi.testclient import TestClient

from loom_v2.observer.app import create_app


def test_production_message_endpoint_is_removed():
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/v1/messages", json={"request_id": "req-api", "conversation_ref": "conversation-api", "text": "hello"})
    assert response.status_code == 404
