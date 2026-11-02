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
    assert "/.well-known/grip" in response.text
    assert "Execution Driver" in response.text
    assert "submit-plan" in response.text
    assert "include_history: true" in response.text
    assert "localStorage" not in response.text
    assert "/api/v1/run-submissions" not in response.text


def test_observer_redirects_task_ui_to_pool_without_a_redirect_loop():
    from loom_v2.settings import Settings
    from loom_v2.pool.app import create_app as pool_app
    from loom_v2.pool.repository import PoolRepository
    settings = Settings(public_base_url="http://observer", pool_public_base_url="http://pool")
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "http://pool/"
    with TestClient(pool_app(PoolRepository(settings=settings), settings=settings)) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200
        assert 'id="next-history"' in response.text
