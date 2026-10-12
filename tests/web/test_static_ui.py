from fastapi.testclient import TestClient

from loom_v2.observer.app import create_app


def test_homepage_contains_external_submission_and_run_drawer():
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "Queue execution" in response.text
    assert "Closure JSON" in response.text
    assert 'id="agent-status"' in response.text
    assert 'id="submission-form"' in response.text
    assert 'id="run-id"' in response.text
    assert "Fake coding-agent" not in response.text


def test_frontend_loads_runtime_agent_status():
    client = TestClient(create_app())
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "/api/v1/runtime" in response.text
    assert "Execution Driver" in response.text
    assert "/api/v1/run-submissions" in response.text
    assert "/api/v1/runs/" in response.text
