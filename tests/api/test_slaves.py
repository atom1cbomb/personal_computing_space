from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from loom_v2.observer.app import create_app
from loom_v2.settings import Settings
from tests.support.grip_client import exchange, resources


@pytest.fixture(params=["memory", "sqlite"])
def client(request, tmp_path):
    database_url = (
        "sqlite+aiosqlite:///:memory:" if request.param == "memory"
        else f"sqlite+aiosqlite:///{tmp_path / 'observer.db'}"
    )
    with TestClient(create_app(settings=Settings(
        database_url=database_url, workspace_id="my-workspace",
        internal_api_secret="internal-only",
    ))) as client:
        yield client


def register(client, slave_id="slave-a", instance="one", workspace="my-workspace", role="slave"):
    response = client.post("/internal/v1/agents/register", headers={
        "X-Loom-Internal-Token": "internal-only",
    }, json={
        "role": role, "agent_id": slave_id, "instance_id": instance,
        "workspace_id": workspace, "endpoint_url": "http://private-worker:8081",
        "protocol_version": "loom.v1",
        "capabilities": {
            "base_actions": ["run_code"],
            "actions": ["run_code", "stale-package-action"],
            "executor_descriptors": [{"package_type": "function", "kind": "process:json_stdio", "version": "1"}],
            "runtime_plugin_descriptors": [{"plugin_id": "http", "supports": [{"package_type": "service", "execution": {"kind": "container:http", "version": "1"}}]}],
            "term_support": [], "private_token": "do-not-expose",
        },
    })
    assert response.status_code == 200
    return response.json()


def test_grip_slaves_empty_and_workspace_bound(client):
    assert resources(client, "slave") == []
    from loom_orchestration import Names
    result = exchange(client, Names(workspace="other").root)
    assert result["error_code"] == "not_found"


def test_public_slaves_scoped_deduplicated_and_safe(client):
    register(client, "slave-b")
    register(client)
    register(client, instance="replacement")
    register(client, "foreign-slave", workspace="other")
    register(client, "driver", role="driver")

    result = resources(client, "slave")
    slaves = [r["metadata"] for r in result]
    assert [slave["slave_id"] for slave in slaves] == ["slave-a", "slave-b"]
    for slave in slaves:
        assert slave["available"] is True
        assert slave["lease_state"] == "active"
        assert slave["last_seen_at"]
        assert slave["base_actions"] == ["run_code"]
        assert slave["executor_descriptors"][0]["kind"] == "process:json_stdio"
        assert slave["runtime_plugin_descriptors"][0]["plugin_id"] == "http"
    for private in ("lease_id", "lease_id_hash", "endpoint_url", "instance_id", "private_token", "do-not-expose", "foreign-slave"):
        assert private not in str(result)
    assert all(r["metadata"]["available"] for r in resources(client, "slave"))
    assert client.get("/internal/v1/agents/slaves").status_code == 401


def test_public_slaves_released_and_expired_are_not_available(client):
    lease = register(client)
    released = client.post("/internal/v1/agents/slave-a/release", headers={
        "X-Loom-Internal-Token": "internal-only",
    }, json={
        "instance_id": "one", "workspace_id": "my-workspace", "role": "slave",
        "lease_id": lease["lease_id"], "epoch": lease["epoch"],
    })
    assert released.status_code == 200
    register(client, "slave-b")
    with patch("loom_v2.observer.repository.datetime") as clock:
        clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=5)
        slaves = [r["metadata"] for r in resources(client, "slave")]
        assert sorted((s["slave_id"], s["lease_state"], s["available"]) for s in slaves) == [
            ("slave-a", "released", False), ("slave-b", "expired", False),
        ]
        assert not any(r["metadata"]["available"] for r in resources(client, "slave"))


def test_grip_is_the_only_public_resource_access_route(client):
    schema = client.get("/openapi.json").json()
    assert "/.well-known/grip" in schema["paths"]
    for removed in ("/api/v1/slaves", "/api/v1/capabilities", "/api/v1/resources", "/api/v1/resources/{resource_id}/activations", "/api/v1/capability-packages/{package_ref}/deactivate", "/api/v1/capability-packages/{package_ref}/auth/start"):
        assert removed not in schema["paths"]


def test_grip_discovery_does_not_mutate_dispatch_cache(client):
    register(client)
    repo = client.app.state.repo
    repo.slave_capabilities = {"slave-a": {"actions": {"foreign-run-action"}}}
    result = resources(client, "slave")
    assert "foreign-run-action" not in str(result)
    assert repo.slave_capabilities == {"slave-a": {"actions": {"foreign-run-action"}}}


def test_public_slaves_uses_latest_offline_instance(client):
    register(client)
    future = datetime.now(timezone.utc) + timedelta(seconds=2)
    with patch("loom_v2.observer.repository.datetime") as clock:
        clock.now.return_value = future
        register(client, instance="newer")
    with patch("loom_v2.observer.repository.datetime") as clock:
        clock.now.return_value = future + timedelta(minutes=5)
        slaves = [r["metadata"] for r in resources(client, "slave")]
    assert len(slaves) == 1
    assert slaves[0]["lease_state"] == "expired"
    seen = datetime.fromisoformat(slaves[0]["last_seen_at"])
    assert seen.replace(tzinfo=timezone.utc) == future
