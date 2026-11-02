"""Real HTTP boundaries with independent Observer/Pool repositories and Slave execution."""
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from loom_orchestration import LoomClient, publish_function
from loom_v2.settings import Settings
from loom_v2.db.session import make_engine
from loom_v2.db.models import RunRow, RunEventRow
from loom_v2.observer.repository import ObserverRepository
from loom_v2.pool.repository import PoolRepository
from loom_v2.observer.app import create_app as observer_app
from loom_v2.pool.app import create_app as pool_app
from loom_v2.pool.client import PoolClient, ResourceClient
from loom_v2.slave.app import create_app as slave_app
from loom_v2.driver.control_client import PoolControlClient
from loom_v2.driver.service import DriverService
from loom_v2.driver.worker import WorkerSession
from tests.api.test_dynamic_nodes import _register_slave
from tests.driver.test_worker_discovery import execute_on, execute_orchestration_on
from tests.db.test_postgres_migrations import postgres_container


class Network(httpx.AsyncBaseTransport):
    def __init__(self):
        self.apps = {}
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.url.host, request.url.path))
        return await httpx.ASGITransport(app=self.apps[request.url.host]).handle_async_request(request)


@pytest.fixture
async def boundary(tmp_path, monkeypatch, request):
    database_url = f"sqlite+aiosqlite:///{tmp_path}/state.db"
    if getattr(request, "param", None) == "postgres":
        import subprocess
        from uuid import uuid4
        container, port = request.getfixturevalue("postgres_container")
        database = "boundary_" + uuid4().hex
        subprocess.run(["docker", "exec", container, "createdb", "-U", "postgres", database], check=True)
        database_url = f"postgresql+asyncpg://postgres:migration-test@127.0.0.1:{port}/{database}"
    settings = Settings(database_url=database_url,
        internal_api_secret="test-boundary", grip_token="test-public",
        public_base_url="http://observer", observer_url="http://observer", pool_url="http://pool", pool_public_base_url="http://pool")
    engine = make_engine(settings.database_url)
    network = Network()
    pool = PoolRepository(engine, settings=settings)
    observer = ObserverRepository(engine, settings=settings,
        task_client=PoolClient(settings.pool_url, settings, transport=network))
    await pool.init_db()
    await observer.init_db()
    pool.resource_client = ResourceClient(settings.observer_url, settings, transport=network)
    network.apps["observer"] = observer_app(observer, settings=settings)
    network.apps["pool"] = pool_app(pool, settings=settings)
    slaves = {name: slave_app(name, settings=settings.model_copy(update={"database_url": "sqlite+aiosqlite:///:memory:"}), observer_transport=network)
        for name in ("slave-a", "slave-npc")}
    network.apps.update(slaves)
    def worker(slave_id, **kwargs):
        return WorkerSession(slave_id, transport=network, **kwargs)
    monkeypatch.setattr("loom_v2.driver.service.WorkerSession", worker)
    control = PoolControlClient(settings.pool_url, driver_id="driver-a", instance_id="a1",
        internal_api_secret=settings.internal_api_secret, transport=network, settings=settings)
    await control.register()
    driver = DriverService(control, None, settings=settings, content_store=pool.content_store)
    async with LoomClient("http://observer", token=settings.grip_token, allow_insecure_grip=True, transport=network,
            trusted_bindings=[{"protocol": "loom-http-json.v1", "audience": pool.names.root, "endpoint": "http://pool/.well-known/grip"}]) as client:
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
        package = await publish_function(client, package_id="boundary-double", package_version="v1", action_name="double",
            source="import sys,json; x=json.load(sys.stdin); print(json.dumps({'value': x['value']*2}))",
            input_schema=schema, output_schema=schema)
        description = await client.describe(package["resource"])
        action = description["resource"]["metadata"]["actions"][0]["uri"]
        runtime = SimpleNamespace(repository=pool, observer=observer, control=control, driver=driver,
            client=client, package=package, action=action, network=network, settings=settings)
        try:
            yield runtime
        finally:
            for worker in driver.workers.values():
                await worker.close()
            await control.close()
            await observer.tasks.close()
            await pool.resource_client.close()
            for app in slaves.values():
                await app.state.service.close()
                await app.state.observer_http.close()
            for app in network.apps.values():
                if hasattr(app.state, "service_access"):
                    await app.state.service_access.http.aclose()
            await engine.dispose()


@pytest.mark.parametrize("execution", ["ordinary", "orchestration"])
async def test_execution_crosses_pool_and_direct_slave_boundary(boundary, execution):
    r = boundary
    await _register_slave(r.observer, "slave-a", "s1")
    completed = await execute_on(r, "slave-a", 3)
    assert completed["outcome"]["value"] == {"value": 6}
    if execution == "orchestration":
        await _register_slave(r.observer, "slave-npc", "s2")
        await execute_orchestration_on(r, "slave-npc", 5)
    assert not r.observer.runs
    assert ("slave-a", "/.well-known/grip") in r.network.calls
    assert ("pool", "/internal/v1/attempts/authorize") in r.network.calls
    assert not any(path.endswith("/forward") for _, path in r.network.calls)
    async with r.repository.sessions() as session:
        assert (await session.scalars(select(RunEventRow))).all()
        assert all(events == [] for events in (await session.scalars(select(RunRow.events))).all())
    run_id = completed["run_id"]
    page = await r.observer.tasks.read_run_history(r.settings.workspace_id, run_id, limit=2)
    next_page = await r.observer.tasks.read_run_history(r.settings.workspace_id, run_id, after=page["next_cursor"], limit=2)
    assert len(page["events"]) == len(next_page["events"]) == 2
    assert page["next_cursor"] == 2 and next_page["next_cursor"] == 4


async def test_observer_has_no_driver_command_route(boundary):
    async with httpx.AsyncClient(transport=boundary.network) as client:
        response = await client.post("http://observer/internal/v1/driver/commands", json={})
        assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["postgres"], indirect=True)
async def test_workspace_activation_does_not_reacquire_pool_run_lock(boundary):
    import asyncio
    r = boundary
    await _register_slave(r.observer, "slave-a", "s1")
    # Both desired-state and health RPCs carry the Run context while Pool owns
    # its row/advisory lock. Observer must lock the activation independently.
    async with asyncio.timeout(15):
        completed = await execute_on(r, "slave-a", 7)
    assert completed["outcome"]["value"] == {"value": 14}
    assert not r.observer.runs


async def test_slave_authorization_rejects_contract_changes_and_non_owner(boundary):
    from copy import deepcopy
    from loom_v2.contracts.agents import AgentRegistration
    r = boundary
    await _register_slave(r.observer, "slave-a", "s1")
    receipt = await r.client.invoke(r.client.names.name("slaves", "slave-a"),
        r.client.names.action("run-package"), {"package": r.package["resource"], "action": r.action,
        "arguments": {"value": 4}, "idempotency_key": "authorization"})
    claim = await r.control.command("execution.claim_next", {})
    run = claim["run"]
    assert receipt["run"].endswith(run["run_id"])
    snapshot = run["committed"]["snapshot"]
    binding = deepcopy(snapshot["compute_bindings"][0])
    binding["expected_activation_revision"] = 1
    request = {"execution_owner": {"instance_id": r.control.instance_id, "lease_id": r.control.lease_id},
        "workspace_id": r.settings.workspace_id, "driver_id": r.control.driver_id,
        "driver_epoch": r.control.driver_epoch, "execution_id": run["execution_id"],
        "execution_epoch": run["execution_epoch"], "attempt_id": run["attempts"][-1]["attempt_id"],
        "target_slave": "slave-a", "closure": snapshot, "binding": binding,
        "action": snapshot["program"]["action_ref"].rsplit("/", 1)[-1]}
    assert (await r.repository.authorize_attempt(request))["authorized"]
    changed = deepcopy(request)
    changed["binding"]["instance_name"] = "different-account"
    with pytest.raises(ValueError, match="attempt_contract_mismatch"):
        await r.repository.authorize_attempt(changed)
    changed = deepcopy(request)
    changed["closure"]["node_input_bindings"] = []
    with pytest.raises(ValueError, match="attempt_contract_mismatch"):
        await r.repository.authorize_attempt(changed)
    peer = await r.repository.register_agent(AgentRegistration(role="driver", agent_id="peer", instance_id="p1",
        workspace_id=r.settings.workspace_id, endpoint_url="http://peer", protocol_version="loom.v1"))
    changed = deepcopy(request)
    changed.update(driver_id=peer.agent_id, driver_epoch=peer.epoch,
        execution_owner={"instance_id": peer.instance_id, "lease_id": peer.lease_id})
    with pytest.raises(ValueError, match="stale_execution_owner"):
        await r.repository.authorize_attempt(changed)
    await r.control.release()
    with pytest.raises(ValueError, match="stale_execution_owner"):
        await r.repository.authorize_attempt(request)
