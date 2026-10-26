"""Public deployment over separate Observer, Slave control and data listeners.

Only the container plugin is a fixture; no embedded Observer shortcut or SSH.
"""
from contextlib import AsyncExitStack
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import pytest

from loom_orchestration import GripError, LoomClient
from loom_v2.contracts.agents import AgentRegistration
from loom_v2.contracts.errors import DomainError
from loom_v2.observer.app import create_app
from loom_v2.observer.grip import GripService
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.app import create_app as create_slave_app
from loom_v2.slave.grip_data import create_data_app
from runtime_plugins.container_http_v1.runtime import RUNTIME_DESCRIPTOR
from tests.support.asgi_server import running_asgi
from tests.support.package_fixture import action_export


@pytest.fixture
async def deployment(tmp_path):
    key = Ed25519PrivateKey.generate()
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(grip_token="owner", coding_agent_backend="none", agent_lease_ttl_seconds=300,
        grip_allow_insecure_services=True, grip_signing_key_file=str(private),
        grip_verification_keys={"service-access-v1": str(public)},
        runtime_plugin_instance_state_dir=str(tmp_path / "state"))
    repo = ObserverRepository(settings=settings)
    app = create_app(repo, settings=settings)
    control = create_slave_app("slave-a", settings=settings)
    slave = control.state.service
    slave.content_store = repo.content_store
    calls = []

    async def provision(package, command):
        calls.append(("provision", command.activation_revision))
        return {"activation_state": "ready", "runtime_profile": {}}

    async def deprovision(package, command):
        calls.append(("deprovision", command.activation_revision))
        return {"activation_state": "stopped"}

    async def invoke(package, export, value, **kwargs):
        calls.append(("invoke", value))
        return {"value": value}

    async def close():
        pass

    slave.runtime_plugin_host = SimpleNamespace(provision=provision, deprovision=deprovision, invoke=invoke, close=close)
    try:
        async with AsyncExitStack() as stack:
            control_url = await stack.enter_async_context(running_asgi(control))
            data_url = await stack.enter_async_context(running_asgi(create_data_app(slave)))
            base = await stack.enter_async_context(running_asgi(app))
            settings.public_base_url = base
            app.state.grip = GripService(app, settings)
            for role, identity, endpoint, capabilities in [
                ("slave", "slave-a", control_url, {"runtime_plugin_descriptors": [RUNTIME_DESCRIPTOR]}),
                ("driver", "driver", "http://unused-driver", {}),
            ]:
                await repo.register_agent(AgentRegistration(role=role, agent_id=identity, instance_id=identity,
                    workspace_id=settings.workspace_id, endpoint_url=endpoint, protocol_version="loom.v1", capabilities=capabilities))
            client = await stack.enter_async_context(LoomClient(base, token="owner", allow_insecure_grip=True))
            target = client.names.name("slaves", "slave-a")
            routes = [
                {"protocol": "loom-http-json.v1", "endpoint": data_url + "/.well-known/grip", "audience": target},
                {"protocol": "loom-wss-stream.v1", "endpoint": data_url.replace("http:", "ws:") + "/grip/stream", "audience": target},
            ]
            assert not app.state.slaves and not app.state.workers and not app.state.embedded
            yield SimpleNamespace(app=app, repo=repo, settings=settings, slave=slave, client=client,
                                  target=target, routes=routes, calls=calls)
    finally:
        await app.state.service_access.http.aclose()
        await app.state.gateway.close()
        await slave.close()


async def publish(d, modes=("task", "request-reply", "byte-stream")):
    c = d.client
    schema = await c.upload_content({"type": "object", "properties": {"value": {"type": "integer"}},
                                    "required": ["value"]}, media_type="application/schema+json")
    contract = await c.upload_content({"schema_version": "io.v1", "input_schema_ref": schema.model_dump(mode="json"),
                                      "output_schema_ref": schema.model_dump(mode="json")})
    exports = []
    for mode in modes:
        descriptor = await c.upload_content({"schema_version": "action.v1", "resource_id": c.names.action(mode), "name": mode},
                                            media_type="application/vnd.loom.action-descriptor+json")
        exports.append(action_export(descriptor.model_dump(mode="json"), contract.model_dump(mode="json"), runtime_binding={"path": "/" + mode, "interaction": mode}))
    result = await c.register_package({"package_id": "preflight-test", "package_version": "v1", "package_type": "service",
        "execution": {"kind": "container:http", "version": "1"}, "action_exports": exports,
        "body": {"image_ref": "registry.example/test@sha256:" + "a" * 64, "container_port": 8080, "health_path": "/healthz"}})
    return result["resource"]


async def check(d, package, instance="default"):
    return await d.client.invoke(d.target, d.client.names.action("check-package"), {"package": package, "instance_name": instance})


async def activate(d, package, instance="default"):
    return await d.client.invoke(d.target, d.client.names.action("activate-package"),
                                {"package": package, "instance_name": instance, "idempotency_key": "same-request"})


async def test_missing_entry_refuses_without_side_effects_then_same_request_deploys(deployment):
    d = deployment
    package = await publish(d)
    result = await check(d, package)
    assert result["ready"] is False
    assert result["required_interactions"] == ["byte-stream", "request-reply", "task"]
    assert {(i["code"], i["interaction"]) for i in result["issues"]} == {
        ("binding_unavailable", "byte-stream"), ("binding_unavailable", "request-reply")}
    assert all(len(i["actions"]) == 1 for i in result["issues"])
    before_events = repr(d.repo.workspace_package_events)
    driver_key = next(k for k in d.repo.agents if k[1] == "driver")
    driver = d.repo.agents.pop(driver_key)
    with pytest.raises(GripError, match="unsupported_binding"):
        await activate(d, package)
    d.repo.agents[driver_key] = driver
    stored = await d.repo.get_capability_package(package)
    assert not await d.repo.list_package_activations(stored)
    assert repr(d.repo.workspace_package_events) == before_events
    assert d.calls == [] and not d.slave.activations

    # This models node setup, not a fallback in the package deployment path.
    d.settings.grip_upstream_bindings = {d.target: d.routes}
    assert (await check(d, package))["ready"] is True
    result = await activate(d, package)
    assert result["state"] == "ready" and result["revision"] == 1
    assert await activate(d, package) == result
    description = await d.client.describe(result["resource"])
    metadata = description["resource"]["metadata"]
    assert metadata["state"] == metadata["runtime_state"] == "ready" and metadata["available"]
    action = next(a["uri"] for a in metadata["actions"] if a["name"] == "request-reply")
    assert await d.client.invoke(result["resource"], action, {"value": 7}) == {"value": 7}
    assert d.calls == [("provision", 1), ("invoke", {"value": 7})]
    assert not d.repo.runs


@pytest.mark.parametrize("mode", ["request-reply", "byte-stream"])
async def test_partial_entry_and_instance_override_are_checked_per_mode(deployment, mode):
    d = deployment
    package = await publish(d)
    d.settings.grip_upstream_bindings = {d.target: [d.routes[0 if mode == "request-reply" else 1]]}
    result = await check(d, package)
    assert len(result["issues"]) == 1 and result["issues"][0]["interaction"] != mode
    instance = d.client.names.name("activations", "preflight-test", "v1", "slave-a", "special")
    d.settings.grip_upstream_bindings[instance] = d.routes
    assert (await check(d, package, "special"))["ready"]
    assert not (await check(d, package))["ready"]


@pytest.mark.parametrize("fault", ["missing-key", "wrong-key-id", "bad-endpoint", "bad-audience"])
async def test_configuration_faults_are_public_and_block_repository_entry(deployment, fault):
    d = deployment
    package_uri = await publish(d, ("request-reply",))
    d.settings.grip_upstream_bindings = {d.target: [dict(d.routes[0])]}
    if fault == "missing-key":
        d.settings.grip_signing_key_file = ""
    elif fault == "wrong-key-id":
        d.settings.grip_signing_key_id = "missing"
    elif fault == "bad-endpoint":
        d.settings.grip_upstream_bindings[d.target][0]["endpoint"] = "https://private:secret@example.test/path?secret=hidden"
    else:
        d.settings.grip_upstream_bindings[d.target][0]["audience"] = d.client.names.root
    result = await check(d, package_uri)
    code = "service_signing_unavailable" if "key" in fault else "binding_invalid"
    assert result["issues"][0]["code"] == code and "secret" not in str(result)
    package = await d.repo.get_capability_package(package_uri)
    with pytest.raises(DomainError) as caught:
        await d.repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running", idempotency_key="driver-desire")
    assert caught.value.envelope.code == "activation_preflight_failed"
    assert caught.value.envelope.details["issues"] == result["issues"]
    assert not await d.repo.list_package_activations(package) and not d.calls


async def test_task_only_package_needs_no_service_keys_or_entry(deployment):
    d = deployment
    d.settings.grip_signing_key_file = ""
    package = await publish(d, ("task",))
    assert await check(d, package) == {"ready": True, "required_interactions": ["task"], "issues": []}
    assert (await activate(d, package))["state"] == "ready"


async def test_route_loss_degrades_discovery_and_blocks_configure_but_allows_stop(deployment):
    d = deployment
    package_uri = await publish(d)
    d.settings.grip_upstream_bindings = {d.target: d.routes}
    result = await activate(d, package_uri)
    target = result["resource"]
    package = await d.repo.get_capability_package(package_uri)
    d.settings.grip_upstream_bindings = {}
    metadata = (await d.client.describe(target, refresh=True))["resource"]["metadata"]
    assert metadata["state"] == "degraded" and metadata["runtime_state"] == "ready"
    assert not metadata["available"] and metadata["unavailable_reason"] == "binding_unavailable"
    assert {a["name"] for a in metadata["actions"] if a["available"]} == {"task"}
    with pytest.raises(GripError, match="unsupported_binding"):
        await d.client.invoke(target, d.client.names.action("configure"),
                              {"expected_revision": 1, "idempotency_key": "configure", "instance_config": {"new": True}})
    current, = await d.repo.list_package_activations(package)
    assert current.activation_revision == 1 and current.instance_config == {}
    assert d.calls == [("provision", 1)]
    d.settings.grip_upstream_bindings = {d.target: d.routes}
    assert (await d.client.describe(target, refresh=True))["resource"]["metadata"]["available"]
    d.settings.grip_upstream_bindings = {}
    stopped = await d.client.invoke(target, d.client.names.action("stop"), {"expected_revision": 1, "idempotency_key": "stop"})
    assert stopped["state"] == "stopped" and d.calls[-1] == ("deprovision", 2)


async def test_upstream_for_one_mode_does_not_hide_direct_binding_for_another(deployment):
    d = deployment
    package = await publish(d)
    d.settings.grip_data_bindings = {d.target: [d.routes[0]]}
    d.settings.grip_upstream_bindings = {d.target: [d.routes[1]]}
    assert (await check(d, package))["ready"]
    node = (await d.client.describe(d.target, refresh=True))["resource"]["metadata"]
    assert all(v["available"] for v in node["service_interactions"].values())


@pytest.mark.parametrize("fault", ["offline", "unsupported"])
async def test_check_reports_node_availability_and_runtime_support(deployment, fault):
    from datetime import datetime, timedelta, timezone

    d = deployment
    package = await publish(d, ("task",))
    agent = next(a for a in d.repo.agents.values() if a["role"] == "slave")
    if fault == "offline":
        agent["last_seen_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    else:
        agent["capabilities"] = {}
    result = await check(d, package)
    assert not result["ready"]
    assert result["issues"] == [{"code": "slave_unavailable" if fault == "offline" else "package_runtime_unsupported",
                                 "interaction": None, "actions": []}]
    assert not d.calls
