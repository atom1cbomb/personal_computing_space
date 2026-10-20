import asyncio
import json

import httpx
import pytest

from loom_orchestration import GripError, LoomClient, Names
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.testing.observer import create_embedded_app
from tests.support.package_fixture import process_package_value


@pytest.fixture
async def setup():
    settings = Settings(grip_token="test-grip-token", public_base_url="http://observer", coding_agent_backend="none")
    repo = ObserverRepository(settings=settings)
    program = await repo.put_content('import json,sys; d=json.load(sys.stdin); print(json.dumps({"value": d["value"] * 2}))', media_type="text/x-python")
    schema = await repo.put_content({"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}, media_type="application/schema+json")
    contract = await repo.put_content({"schema_version": "io.v1", "input_schema_ref": schema.model_dump(mode="json")}, media_type="application/vnd.loom.io-contract+json")
    app = create_embedded_app(repo, settings=settings)
    async with LoomClient("http://observer", token=settings.grip_token, transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
        package = await client.register_package(process_package_value(repo, action_ref="grip://loom.local/workspaces/workspace-default/actions/double", program_content_ref=program, io_contract_ref=contract, package_id="grip-double"))
        yield app, repo, client, package
    await app.state.driver.stop_execution_loop()
    for slave in app.state.slaves.values():
        await slave.close()


async def wait_result(app, client, run):
    app.state.driver.start_execution_loop()
    for _ in range(200):
        result = await client.invoke(run, client.names.action("read"), {})
        if result["state"] in {"completed", "failed", "cancelled"}:
            return result
        await asyncio.sleep(.01)
    pytest.fail("execution did not settle")


async def identities(client):
    slave = next(r for r in await client.resources(kind="slave") if r["uri"].endswith("slave-a"))
    package, = await client.resources(kind="capability-package")
    return slave["uri"], package["uri"], package["metadata"]["actions"][0]["uri"]


async def test_describe_run_package_and_read_real_result(setup):
    app, repo, client, package = setup
    slave, package_uri, action = await identities(client)
    description = await client.describe(slave)
    assert description["resource"]["kind"] == "slave"
    assert description["offers"][0]["bindings"][0]["endpoint"] == "http://observer/.well-known/grip"
    data = {"package": package_uri, "action": action, "arguments": {"value": 21}, "idempotency_key": "double-1"}
    first = await client.invoke(slave, client.names.action("run-package"), data)
    assert await client.invoke(slave, client.names.action("run-package"), data) == first
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(slave, client.names.action("run-package"), {**data, "arguments": {"value": 99}})
    result = await wait_result(app, client, first["run"])
    assert result["state"] == "completed", result
    assert result["outcome"]["value"] == {"value": 42}
    activations = await client.resources(kind="activation")
    assert len(activations) == 1
    assert activations[0]["metadata"]["package"] == package_uri
    assert "instance_config" not in json.dumps(activations)


async def test_named_activation_invocation_keeps_instance_and_revision(setup, monkeypatch):
    app, repo, client, _ = setup
    slave, package, action = await identities(client)
    activated = await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": "named", "idempotency_key": "activate-named",
    })
    assert activated["state"] == "ready"
    target = activated["resource"]
    description = await client.describe(target)
    assert description["resource"]["metadata"]["instance_name"] == "named"
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(target, action, {"arguments": {"value": 2}, "expected_revision": 999, "idempotency_key": "stale"})
    async def no_provision(*args, **kwargs):
        pytest.fail("invoking existing activation must not provision")
    monkeypatch.setattr(app.state.slaves["slave-a"], "provision", no_provision)
    run = await client.invoke(target, action, {"arguments": {"value": 4}, "expected_revision": activated["revision"], "idempotency_key": "named-call"})
    result = await wait_result(app, client, run["run"])
    assert result["state"] == "completed", result
    assert result["outcome"]["value"] == {"value": 8}
    stopped = await client.invoke(target, client.names.action("stop"), {"expected_revision": activated["revision"], "idempotency_key": "stop-named"})
    assert stopped["state"] == "stopped"
    assert await client.invoke(target, client.names.action("stop"), {"expected_revision": activated["revision"], "idempotency_key": "stop-named"}) == stopped
    assert (await client.describe(target))["resource"]["metadata"]["state"] == "stopped"
    with pytest.raises(GripError, match="unsupported_action"):
        await client.invoke(target, action, {"arguments": {}, "expected_revision": stopped["revision"], "idempotency_key": "stopped"})


async def test_schema_validation_exact_action_and_schema_resources(setup):
    app, repo, client, _ = setup
    slave, package, action = await identities(client)
    for bad in ({"value": "wrong"}, {}):
        with pytest.raises(GripError, match="invalid_input"):
            await client.invoke(slave, client.names.action("run-package"), {"package": package, "action": action, "arguments": bad, "idempotency_key": "invalid"})
    with pytest.raises(GripError, match="unsupported_action"):
        await client.invoke(slave, client.names.name("other", "run-package", "v1"), {})
    description = await client.describe(slave)
    schema = await client.invoke(description["offers"][0]["action"]["input_schema"], client.names.action("read"), {})
    assert "package" in schema["required"]
    assert not repo.runs


async def test_wire_authentication_validation_and_deadline(setup):
    app, repo, client, _ = setup
    base = {"version": "grip.v1", "request_id": "wire-test", "method": "DESCRIBE", "auth": {"scheme": "bearer", "credential": "test-grip-token"}, "target": client.names.root, "accepted_protocols": ["loom-http-json.v1"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://observer") as raw:
        cases = [({"extra": 1}, "invalid_message"), ({"version": "old"}, "unsupported_version"),
                 ({"auth": {"scheme": "bearer", "credential": "wrong"}}, "authentication_failed"),
                 ({"auth": {**base["auth"], "tenant": Names(workspace="other").root}}, "forbidden"),
                 ({"deadline": "2020-01-01T00:00:00Z"}, "deadline_exceeded"),
                 ({"accepted_protocols": ["https-json.v1"]}, "unsupported_binding"),
                 ({"action": None}, "invalid_message"), ({"target": base["target"] + "?x=1"}, "invalid_message")]
        for changes, code in cases:
            response = await raw.post("/.well-known/grip", json={**base, **changes})
            assert response.status_code == 200
            assert response.json()["error_code"] == code, response.text
        missing = dict(base); missing.pop("auth")
        response = await raw.post("/.well-known/grip", json=missing, headers={"Authorization": "Bearer test-grip-token"})
        assert response.json()["error_code"] == "authentication_required"
        for content in ('{"request_id":"a","request_id":"b"}', '{"request_id":3}', '{broken'):
            assert (await raw.post("/.well-known/grip", content=content)).status_code == 400
        response = await raw.post("/.well-known/grip", json=base)
        assert response.headers["cache-control"] == "private, no-cache"


async def test_resource_binding_is_explicit_and_ambiguous_instances_are_not_chosen(setup):
    app, repo, client, raw_package = setup
    slave, package, action = await identities(client)
    resource_id = "grip://loom.local/workspaces/workspace-default/resources/vm/test"
    await repo.register_resource("workspace-default", {
        "resource_ref": {"resource_id": resource_id},
        "package_ref": {"resource_id": raw_package["resource"], "version_or_digest": raw_package["package_digest"]},
        "state": "running",
    })
    target = resource_id
    assert (await client.describe(target))["offers"] == []
    activation = await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": "vm-one", "target": target, "idempotency_key": "vm-one",
    })
    assert len((await client.describe(target))["offers"]) == 1
    run = await client.invoke(target, action, {"arguments": {"value": 3}, "expected_revision": activation["revision"], "idempotency_key": "vm-call"})
    assert (await wait_result(app, client, run["run"]))["outcome"]["value"] == {"value": 6}
    await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": "vm-two", "target": target, "idempotency_key": "vm-two",
    })
    description = await client.describe(target)
    assert description["offers"] == []
    assert len(description["resource"]["metadata"]["activations"]) == 2


async def test_activation_stopped_after_enqueue_is_rejected_at_slave(setup):
    app, repo, client, _ = setup
    slave, package, action = await identities(client)
    activation = await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": "fenced", "idempotency_key": "fenced-start",
    })
    run = await client.invoke(activation["resource"], action, {"arguments": {"value": 5}, "expected_revision": activation["revision"], "idempotency_key": "fenced-call"})
    await client.invoke(activation["resource"], client.names.action("stop"), {"expected_revision": activation["revision"], "idempotency_key": "fenced-stop"})
    result = await wait_result(app, client, run["run"])
    assert result["state"] == "failed", result
    assert app.state.slaves["slave-a"].attempts == {}


async def test_directory_and_queued_action_survive_repository_reload(setup, tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from loom_v2.contracts.types import CapabilityHealthReport
    from loom_v2.observer.app import create_app
    from tests.api.test_dynamic_nodes import _register_slave

    _, original, _, raw = setup
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'grip.db'}")
    settings = Settings(grip_token="persistent", public_base_url="http://observer", coding_agent_backend="none")
    repo = ObserverRepository(engine, content_store=original.content_store, settings=settings)
    await repo.init_db()
    await _register_slave(repo, "slave-a", "persistent-slave")
    manifest = {key: raw[key] for key in ("package_id", "package_version", "package_type", "execution", "action_exports", "body")}
    # Test descriptors registered in memory are normally uploaded to ContentStore.
    ref = raw["action_exports"][0]["action_descriptor_ref"]
    from loom_v2.contracts.types import ResourceRef
    definition = await original.get_action_descriptor(ResourceRef.model_validate(ref))
    persisted_ref = await repo.put_content(definition, media_type="application/vnd.loom.action-descriptor+json")
    manifest["action_exports"] = [{**raw["action_exports"][0], "action_descriptor_ref": persisted_ref.model_dump(mode="json")}]
    package = await repo.register_workspace_package("workspace-default", manifest)
    await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running", idempotency_key="persist", instance_name="saved")
    await repo.record_capability_health(CapabilityHealthReport(report_id="saved-ready", package_version_ref=package.version_ref, package_digest=package.package_digest, target_slave="slave-a", instance_name="saved", activation_state="ready", activation_revision=1))
    await repo.register_resource("workspace-default", {"resource_ref": {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/persisted"}, "package_ref": {"resource_id": package.version_ref, "version_or_digest": package.package_digest}})
    app = create_app(repo, settings=settings)
    async with LoomClient("http://observer", token="persistent", allow_insecure_grip=True, transport=httpx.ASGITransport(app=app)) as client:
        slave, package_uri, action = await identities(client)
        run = await client.invoke(slave, client.names.action("run-package"), {"package": package_uri, "action": action, "arguments": {"value": 1}, "idempotency_key": "persist-call"})
    restored = ObserverRepository(engine, content_store=original.content_store, settings=settings)
    await restored.init_db()
    restored_app = create_app(restored, settings=settings)
    async with LoomClient("http://observer", token="persistent", allow_insecure_grip=True, transport=httpx.ASGITransport(app=restored_app)) as client:
        assert len(await client.resources(kind="activation")) == 1
        assert len(await client.resources(kind="resource")) == 1
        result = await client.invoke(run["run"], client.names.action("read"), {})
        assert result["state"] == "committed"
        cancelled = await client.invoke(run["run"], client.names.action("cancel"), {})
        assert cancelled["state"] == "cancelled"
    await engine.dispose()


async def test_named_activation_binding_survives_http_worker_envelope(setup, monkeypatch):
    from loom_v2.driver.worker import WorkerSession
    from loom_v2.slave.app import create_app as slave_app
    app, repo, client, _ = setup
    slave, package, action = await identities(client)
    activation = await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": "http-named", "idempotency_key": "http-activate",
    })
    remote = slave_app("slave-a", settings=repo.settings)
    await remote.state.service.close()
    remote.state.service = app.state.slaves["slave-a"]
    async def no_provision(*args, **kwargs):
        pytest.fail("HTTP dispatch must reuse the pinned activation")
    monkeypatch.setattr(remote.state.service, "provision", no_provision)
    worker = WorkerSession("slave-a", "http://slave-a", transport=httpx.ASGITransport(app=remote))
    app.state.driver.workers["slave-a"] = worker
    try:
        run = await client.invoke(activation["resource"], action, {"arguments": {"value": 6}, "expected_revision": activation["revision"], "idempotency_key": "http-call"})
        result = await wait_result(app, client, run["run"])
        assert result["state"] == "completed", result
        assert result["outcome"]["value"] == {"value": 12}
    finally:
        await worker.close()
        await remote.state.observer_http.close()


async def test_foreign_workspace_and_unconfigured_auth_are_not_visible(setup):
    app, repo, client, raw = setup
    await repo.register_workspace_package("foreign", {**raw, "package_id": "foreign-package"})
    assert len(await client.resources(kind="capability-package")) == 1
    base = {"version": "grip.v1", "request_id": "foreign", "method": "DESCRIBE", "auth": {"scheme": "bearer", "credential": "test-grip-token"}, "accepted_protocols": ["loom-http-json.v1"], "target": Names(workspace="foreign").root}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://observer") as raw_client:
        response = await raw_client.post("/.well-known/grip", json=base)
        assert response.json()["error_code"] == "not_found"
        app.state.grip.settings.grip_token = ""
        base["target"] = client.names.root
        assert (await raw_client.post("/.well-known/grip", json=base)).json()["error_code"] == "authentication_failed"


async def test_pending_auth_instance_offers_auth_action_and_forwards_revision(setup):
    from types import SimpleNamespace
    from loom_v2.contracts.types import CapabilityHealthReport

    app, repo, client, raw = setup
    package = await repo.register_workspace_package("workspace-default", {
        "package_id": "authenticated-service", "package_version": "v1", "package_type": "service",
        "execution": {"kind": "container:http", "version": "1"},
        "action_exports": [{**raw["action_exports"][0], "runtime_binding": {"path": "/call"}}],
        "body": {"image_ref": "example/service@sha256:" + "a" * 64, "container_port": 8080,
                 "health_path": "/healthz", "authentication": {"start_path": "/auth/start", "status_path": "/auth/status"}},
    })
    activation = await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running", idempotency_key="auth-prepare", instance_name="login")
    def report(state, report_id):
        return CapabilityHealthReport(report_id=report_id, package_version_ref=package.version_ref, package_digest=package.package_digest,
                                      target_slave="slave-a", instance_name="login", activation_state=state, activation_revision=activation.activation_revision)
    await repo.record_capability_health(report("pending_auth", "pending-auth"))
    target = app.state.grip.activation_uri(package, activation)
    description = await client.describe(target)
    actions = [offer["action"]["uri"] for offer in description["offers"]]
    assert client.names.action("auth-start") in actions
    assert all("/exports/" not in action for action in actions)
    declared, = description["resource"]["metadata"]["actions"]
    assert declared["available"] is False
    assert declared["unavailable_reason"] == "activation_pending_auth"
    async def auth_start(**kwargs):
        assert kwargs["activation_revision"] == activation.activation_revision
        assert kwargs["instance_name"] == "login"
        return report("ready", "authenticated")
    app.state.workers["slave-a"] = SimpleNamespace(auth_start=auth_start)
    result = await client.invoke(target, client.names.action("auth-start"), {"expected_revision": activation.activation_revision})
    assert result["health_reports"][0]["activation_state"] == "ready"
    assert any("/exports/" in offer["action"]["uri"] for offer in (await client.describe(target))["offers"])
    ready, = (await client.describe(target))["resource"]["metadata"]["actions"]
    assert ready == {**declared, "available": True, "unavailable_reason": None}
    await repo.record_capability_health(report("failed", "activation-failed"))
    failed = await client.describe(target)
    assert failed["resource"]["metadata"]["actions"] == [{**declared, "unavailable_reason": "activation_failed"}]
    assert all("/exports/" not in offer["action"]["uri"] for offer in failed["offers"])


async def test_browser_auth_cannot_follow_a_concurrent_activation_revision(setup, monkeypatch):
    from loom_v2.contracts.types import CapabilityHealthReport

    app, repo, client, raw = setup
    package = await repo.register_workspace_package("workspace-default", {
        "package_id": "browser-service", "package_version": "v1", "package_type": "service",
        "execution": {"kind": "container:http", "version": "1"},
        "action_exports": [{**raw["action_exports"][0], "runtime_binding": {"path": "/call"}}],
        "body": {"image_ref": "example/service@sha256:" + "a" * 64, "container_port": 8080,
                 "health_path": "/healthz", "authentication": {
                     "start_path": "/auth/start", "status_path": "/auth/status", "browser_path": "/auth/browser",
                     "client": {"kind": "local-browser-bridge", "protocol_version": "1", "entrypoint": "client/auth.py"}}},
    })
    activation = await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running", idempotency_key="browser-start", instance_name="browser")
    await repo.record_capability_health(CapabilityHealthReport(
        report_id="browser-pending", package_version_ref=package.version_ref, package_digest=package.package_digest,
        target_slave="slave-a", instance_name="browser", activation_state="pending_auth", activation_revision=activation.activation_revision,
    ))
    target = app.state.grip.activation_uri(package, activation)
    start = app.state.browser_auth.start

    async def race(request, package, targets, instance_name, *, expected_revision):
        assert expected_revision == 1
        await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="stopped", idempotency_key="concurrent-stop", instance_name="browser")
        return await start(request, package, targets, instance_name, expected_revision=expected_revision)

    monkeypatch.setattr(app.state.browser_auth, "start", race)
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(target, client.names.action("auth-start"), {"expected_revision": 1})
    assert not app.state.browser_auth.sessions
