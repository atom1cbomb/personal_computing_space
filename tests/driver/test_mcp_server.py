from fastapi.testclient import TestClient

from loom_v2.testing.observer import create_embedded_app as create_app
from loom_v2.observer.repository import ObserverRepository


def test_observer_does_not_expose_public_mcp_transport():
    client = TestClient(create_app(ObserverRepository()))
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response.status_code == 404
