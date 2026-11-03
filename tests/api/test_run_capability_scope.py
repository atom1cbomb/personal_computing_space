import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.agents import AgentRegistration
from loom_v2.contracts.types import CapabilityPackageVersion
from loom_v2.observer.activation import execute_activation_targets
from loom_v2.pool.repository import PoolRepository
from loom_v2.testing.observer import create_embedded_app
from tests.api.test_capability_packages import _program_ref, _empty_io_contract_ref
from tests.support.package_fixture import process_package_value
from tests.support.grip_client import invoke, resources


async def candidate(repo):
    run = await repo.open_run("private-owner", "private-owner", "private capability")
    manifest = process_package_value(repo, package_id="scope-demo",
        action_ref=repo.names.action("scope-demo"), program_content_ref=await _program_ref(repo),
        io_contract_ref=await _empty_io_contract_ref(repo))
    package = CapabilityPackageVersion.model_validate({**manifest, "source_run_ref": run.run_id,
        "workspace_id": repo.settings.workspace_id})
    run.capability_packages.append(package)
    await repo._persist(run)
    return run, package


@pytest.mark.asyncio
async def test_agent_migrates_by_publishing_reactivating_then_stopping_private_instance():
    repo = PoolRepository()
    run, package = await candidate(repo)
    app = create_embedded_app(repo)
    reports = await execute_activation_targets(repo, package, ["slave-a"], desired_state="running",
        workspace_id=repo.settings.workspace_id, workers={}, slaves=app.state.slaves, idempotency_key="private-start")
    assert reports[0]["activation_state"] == "ready", reports
    assert "scope-demo" not in app.state.slaves["slave-a"].capability_snapshot()
    await repo.cancel_run(run.run_id)
    with TestClient(app) as client:
        assert client.get(f"/api/v1/capability-packages/{package.version_ref}").status_code == 404
        private = client.get(f"/api/v1/capability-packages/{package.version_ref}", params={"run_id": run.run_id})
        assert private.status_code == 200, private.text
        assert resources(client, "capability/activation") == []
        published = client.post(f"/api/v1/capability-packages/{package.version_ref}/promote",
            params={"run_id": run.run_id}, json={"approved_digest": package.package_digest, "idempotency_key": "publish-private"})
        assert published.status_code == 200, published.text
        shared = published.json()
        activation = invoke(client, repo.names.name("slaves", "slave-a"), repo.names.action("activate-package"),
            {"package": shared["resource"], "idempotency_key": "shared-start", "instance_name": "shared"})
        assert activation["state"] in {"ready", "degraded"}
        stop_body = {"target_slave": "slave-a", "expected_revision": 1, "idempotency_key": "private-stop"}
        stopped = client.post(f"/api/v1/runs/{run.run_id}/capability-packages/{package.version_ref}/stop", json=stop_body)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["health_reports"][0]["activation_state"] == "stopped"
        states = client.get(f"/api/v1/capability-packages/{shared['resource']}").json()["activations"]
        assert len(states) == 1 and states[0]["activation_state"] == "ready"
        assert client.post(f"/api/v1/runs/{run.run_id}/capability-packages/{shared['resource']}/stop", json=stop_body).status_code == 409


@pytest.mark.asyncio
async def test_private_health_report_requires_owner_run_context():
    repo = PoolRepository()
    run, package = await candidate(repo)
    app = create_embedded_app(repo)
    await repo.set_capability_desired(package.version_ref, "slave-a", run_id=run.run_id,
        desired_state="running", idempotency_key="private-start")
    lease = await repo.register_agent(AgentRegistration(role="slave", agent_id="slave-a", instance_id="health-slave",
        workspace_id=repo.settings.workspace_id, endpoint_url="http://slave-a", protocol_version="loom.v1"))
    payload = {"agent_id": lease.agent_id, "instance_id": lease.instance_id, "lease_id": lease.lease_id,
        "epoch": lease.epoch, "workspace_id": lease.workspace_id, "report": {
            "report_id": "private-health", "package_version_ref": package.version_ref, "package_digest": package.package_digest,
            "target_slave": "slave-a", "activation_revision": 1, "activation_state": "ready"}}
    with TestClient(app) as client:
        assert client.post("/internal/v1/capability-health", json=payload).status_code == 404
        response = client.post("/internal/v1/capability-health", json={**payload, "run_id": run.run_id})
        assert response.status_code == 200, response.text
        assert response.json()["activation"]["activation_state"] == "ready"
        assert resources(client, "capability/activation") == []


@pytest.mark.asyncio
async def test_run_catalog_rejects_foreign_workspace_context():
    repo = PoolRepository()
    await repo.open_run("foreign-run", "foreign-run", "foreign", workspace_id="foreign")
    with TestClient(create_embedded_app(repo)) as client:
        assert client.get("/api/v1/runs/foreign-run/capability-packages").status_code == 404
        assert client.get("/api/v1/capability-packages", params={"run_id": "foreign-run"}).status_code == 404
