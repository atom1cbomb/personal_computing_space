"""Discover newly registered executors while the same Driver keeps running."""

from types import SimpleNamespace

import httpx
import pytest

from loom_orchestration import LoomClient, OrchestratorPackage, SubmissionBuilder, publish_function
from loom_orchestration import ResourceRef as PublicResourceRef
from loom_v2.contracts.types import ResourceRef
from loom_v2.driver.control_client import ObserverControlClient
from loom_v2.driver.service import DriverService
from loom_v2.driver.worker import WorkerSession
from loom_v2.observer.app import create_app as create_observer_app
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.app import create_app as create_slave_app
from tests.api.test_dynamic_nodes import _register_slave


@pytest.fixture
async def discovery_runtime(monkeypatch):
    settings = Settings(internal_api_secret="discovery-secret", grip_token="discovery-token",
                        observer_url="http://observer")
    repository = ObserverRepository(settings=settings)
    observer = create_observer_app(repository, settings=settings)
    transport = httpx.ASGITransport(app=observer)
    control = ObserverControlClient("http://observer", driver_id="discovery-driver",
        instance_id="discovery-instance", internal_api_secret=settings.internal_api_secret,
        transport=transport, settings=settings)
    await control.register()
    slaves = {name: create_slave_app(name, settings=settings)
              for name in ("slave-a", "slave-npc")}

    async def forward_to_slave(slave_id, path, payload):
        async with httpx.AsyncClient(base_url="http://" + slave_id,
                transport=httpx.ASGITransport(app=slaves[slave_id])) as http:
            response = await http.post(path, json=payload,
                headers={"X-Loom-Internal-Token": settings.internal_api_secret})
            response.raise_for_status()
            return response.json()

    monkeypatch.setattr(observer.state.slave_gateway, "forward", forward_to_slave)

    def worker_session(slave_id, **kwargs):
        # Keep real provisioning/execution, with HTTP routed to the test Slave.
        kwargs.pop("observer_url")
        return WorkerSession(slave_id, "http://" + slave_id,
            transport=httpx.ASGITransport(app=slaves[slave_id]), **kwargs)

    monkeypatch.setattr("loom_v2.driver.service.WorkerSession", worker_session)
    driver = DriverService(control, provider=None, settings=settings,
                           content_store=repository.content_store)
    async with LoomClient("http://observer", token=settings.grip_token,
                          allow_insecure_grip=True, transport=transport) as client:
        schema = {"type": "object", "properties": {"value": {"type": "integer"}},
                  "required": ["value"]}
        package = await publish_function(client, package_id="discovery-double",
            package_version="v1", action_name="double",
            source="import json,sys; d=json.load(sys.stdin); print(json.dumps({'value':d['value']*2}))",
            input_schema=schema, output_schema=schema)
        description = await client.describe(package["resource"])
        action = description["resource"]["metadata"]["actions"][0]["uri"]
        try:
            yield SimpleNamespace(repository=repository, control=control, driver=driver,
                                  client=client, package=package, action=action)
        finally:
            for worker in driver.workers.values():
                await worker.close()
            await control.close()
            for slave in slaves.values():
                await slave.state.service.close()
                await slave.state.observer_http.close()


async def execute_on(runtime, slave_id, value):
    receipt = await runtime.client.invoke(
        runtime.client.names.name("slaves", slave_id), runtime.client.names.action("run-package"),
        {"package": runtime.package["resource"], "action": runtime.action,
         "arguments": {"value": value}, "idempotency_key": f"{slave_id}-{value}"})
    run_id = receipt["run"].rsplit("/", 1)[-1]
    claim = await runtime.control.command("execution.claim_next", {})
    assert claim["run"]["run_id"] == run_id
    completed = await runtime.driver._execute_and_wait_remote(
        run_id, "", expected_epoch=runtime.control.driver_epoch)
    assert completed["state"] == "completed"
    assert completed["outcome"]["value"] == {"value": value * 2}
    assert completed["attempts"][-1]["target"] == slave_id
    return completed


class SingleNodeExecutor:
    """Replace the Docker process, keeping real node selection and execution."""

    def __init__(self, package, descriptor):
        self.package = ResourceRef(resource_id=package["resource"],
                                   version_or_digest=package["package_digest"])
        self.descriptor = ResourceRef.model_validate(descriptor)

    async def run(self, _program, input_ref, *, emit_node, result, **_callbacks):
        handle = await emit_node("double", self.package, self.descriptor, [input_ref])
        return await result(handle)


async def execute_orchestration_on(runtime, slave_id, value):
    client = runtime.client
    description = await client.describe(runtime.package["resource"])
    action = description["resource"]["metadata"]["actions"][0]
    node_ref = PublicResourceRef(resource_id=runtime.package["resource"],
                                version_or_digest=runtime.package["package_digest"])
    source = await client.upload_content(
        'async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":\n'
        f'    handle = ctx.emit_node("double", {node_ref.model_dump(mode="json")!r}, '
        f'{action["action_descriptor_ref"]!r}, [input_ref])\n'
        '    return await ctx.result(handle)\n', media_type="text/x-python")
    descriptor = await client.upload_content({"schema_version": "action.v1",
        "resource_id": client.names.name("actions", "discovery-orchestrator", "orchestrate"),
        "name": "orchestrate"}, media_type="application/vnd.loom.action-descriptor+json")
    package = await client.register_package(OrchestratorPackage(
        package_id="discovery-orchestrator", program_content_ref=source,
        action_descriptor_ref=descriptor, io_contract_ref=PublicResourceRef.model_validate(action["io_contract_ref"]),
        allowed_node_package_refs=[node_ref],
        max_nodes=1, max_live_nodes=1).manifest())
    runtime.driver.orchestration_executor = SingleNodeExecutor(
        runtime.package, action["action_descriptor_ref"])
    input_ref = await client.upload_content({"value": value})
    plan = SubmissionBuilder(goal="execute on newly registered slave",
        body={"program": {"action_ref": client.names.name("actions", "discovery-orchestrator", "orchestrate"),
                          "io_contract_ref": action["io_contract_ref"]},
              "compute": {"requirements": [{"view": "systems", "key": "loom.data.locality.v1", "value": slave_id}]}},
        orchestrator_package={"resource_id": package["resource"],
                              "version_or_digest": package["package_digest"]},
        input_binding=input_ref).build(request_id="discovery-orchestration")
    receipt = await client.submit(request_id="discovery-orchestration", plan=plan)
    claim = await runtime.control.command("execution.claim_next", {})
    assert claim["run"]["run_id"] == receipt["run_id"]
    completed = await runtime.driver._execute_and_wait_remote(
        receipt["run_id"], "", expected_epoch=runtime.control.driver_epoch)
    assert completed["state"] == "completed"
    assert completed["outcome"]["value"] == {"value": value * 2}
    assert completed["attempts"][-1]["target"] == slave_id


@pytest.mark.parametrize("execution", ["direct", "orchestrated"])
async def test_new_slave_executes_after_worker_cache_is_populated(discovery_runtime, execution):
    runtime = discovery_runtime
    await _register_slave(runtime.repository, "slave-a", "initial-instance")
    await execute_on(runtime, "slave-a", 3)
    original_worker = runtime.driver.workers["slave-a"]

    await _register_slave(runtime.repository, "slave-npc", "new-instance")
    if execution == "direct":
        await execute_on(runtime, "slave-npc", 7)
    else:
        await execute_orchestration_on(runtime, "slave-npc", 7)

    assert runtime.driver.workers["slave-a"] is original_worker
