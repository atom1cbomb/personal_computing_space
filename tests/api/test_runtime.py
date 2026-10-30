from tests.support.grip_client import submit_plan
from fastapi.testclient import TestClient

from loom_v2.testing.observer import create_embedded_app as create_app
from loom_v2.observer.app import create_app as create_production_app
from loom_v2.settings import Settings


def test_runtime_reports_execution_capabilities_without_model_details():
    response = TestClient(create_app()).get("/api/v1/runtime")

    assert response.status_code == 200
    assert response.json() == {"execution": True, "orchestration": True}


def test_runtime_does_not_expose_test_provider_details():
    response = TestClient(create_app()).get("/api/v1/runtime")

    assert response.status_code == 200
    assert "coding_agent_backend" not in response.json()
    assert "model" not in response.json()


def test_runtime_reports_driver_unavailable_in_split_deployment(monkeypatch):
    monkeypatch.setenv("LOOM_INTERNAL_API_SECRET", "test-internal-secret")

    response = TestClient(create_production_app()).get("/api/v1/runtime")

    assert response.status_code == 503
    assert response.json() == {"code": "driver_unavailable", "retryable": True}


def test_none_backend_keeps_external_submission_available_without_provider():
    app = create_app(settings=Settings(coding_agent_backend="none"))
    assert app.state.provider is None
    response = submit_plan(TestClient(app), **{
            "idempotency_key": "none-backend-request",
            "plan": {"closure_id": "none-backend-closure", "goal": "execute uploaded plan", "body": {"closure_id": "none-backend-closure"}},
        },
    )
    assert response["kind"] == "reply"
