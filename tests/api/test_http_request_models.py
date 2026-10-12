from fastapi.testclient import TestClient

from loom_v2.driver.app import create_app as create_driver_app
from loom_v2.observer.app import create_app as create_observer_app


def test_public_message_endpoint_is_removed() -> None:
    with TestClient(create_observer_app()) as client:
        response = client.post("/api/v1/messages", json={})
    assert response.status_code == 404


def test_driver_message_endpoint_is_removed() -> None:
    with TestClient(create_driver_app()) as client:
        response = client.post("/driver/v1/messages", json={"text": "hello"})
    assert response.status_code == 404
