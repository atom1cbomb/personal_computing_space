"""Real manifests -> GRIP -> Driver/Slave -> container:http -> real HTTP gateways.

Only Docker and external providers are fixtures; no live infrastructure is used.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from importlib import import_module
import json
import shutil
from pathlib import Path
from threading import Thread

import httpx
import pytest

from loom_orchestration import GripError, LoomClient, ResourceRef, ServicePackage
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.runtime_plugins import RuntimePluginDescriptor, RuntimePluginHost
from loom_v2.testing.observer import create_embedded_app, seed_embedded_slaves
from runtime_plugins.container_http_v1.runtime import DockerCommandResult, DockerContainerHTTPRuntimeV1, RUNTIME_DESCRIPTOR

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("kubernetes_http", "outline_http", "suanlicloud_http", "vm_ssh_http", "suanli_vm_ssh_http")
IMAGE = "registry.example/loom/test@sha256:" + "a" * 64


async def wait_result(app, client, run):
    app.state.driver.start_execution_loop()
    for _ in range(100):
        result = await client.invoke(run, client.names.action("read"), {})
        if result["state"] in {"completed", "failed", "cancelled", "awaiting_decision"}:
            return result
        await asyncio.sleep(.02)
    pytest.fail(f"execution did not settle: {result}")


@contextmanager
def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class DockerFixture:
    def __init__(self):
        self.containers = {}
        self.calls = []

    async def run(self, args, *, timeout):
        self.calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return DockerCommandResult(0, json.dumps([args[2]]))
        if args[0] == "inspect":
            value = self.containers.get(args[1])
            return DockerCommandResult(0, json.dumps([value])) if value else DockerCommandResult(1)
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
            self.containers[name] = {"Config": {"Labels": labels, "Image": args[-1]}, "State": {"Running": False}}
        elif args[0] == "start":
            self.containers[args[1]]["State"]["Running"] = True
        elif args[0] == "rm":
            self.containers.pop(args[-1], None)
        return DockerCommandResult(0)


class LocalGatewayTransport(httpx.AsyncBaseTransport):
    def __init__(self, port):
        self.port = port
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.url.host, request.url.path, request.content))
        # A new transport per request matches the runtime client's ownership.
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.request(request.method, f"http://127.0.0.1:{self.port}{request.url.path}", content=request.content, headers=request.headers)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)


class RuntimeAdapter:
    descriptor = RuntimePluginDescriptor.from_mapping(RUNTIME_DESCRIPTOR)

    def __init__(self, runtime):
        self.runtime = runtime

    async def provision(self, package, command):
        return await self.runtime.provision(package.model_dump(mode="json"), command.model_dump(mode="json"))

    async def invoke(self, package, export, payload, **kwargs):
        return await self.runtime.invoke(package.model_dump(mode="json"), export.model_dump(mode="json"), payload, **kwargs)

    async def auth_start(self, package, activation):
        return await self.runtime.auth_start(package.model_dump(mode="json"), activation)

    async def inspect(self, activation):
        return await self.runtime.inspect(activation)

    async def deprovision(self, command):
        return await self.runtime.deprovision(command.model_dump(mode="json"))

    async def reconcile(self, activations):
        return await self.runtime.reconcile(activations)

    async def close(self):
        await self.runtime.close()


def gateway_fixture(directory, tmp_path, monkeypatch):
    module = import_module(f"capability_services.{directory}.server")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"host": "192.0.2.7", "port": 22, "user": "root"}))
    external = {"failed": False, "invalid": False, "calls": []}
    if directory == "kubernetes_http":
        (credentials / "kubeconfig").write_text("fixture")
        (credentials / "policy.json").write_text(json.dumps({"namespace": "test"}))
        command = tmp_path / "kubectl"
        command.write_text('#!/bin/sh\ncase "$*" in *"auth can-i"*) echo yes;; *) echo \'{"items":[]}\';; esac\n')
        command.chmod(0o755)
        gateway = module.KubernetesGateway(credential_root=credentials, kubectl=str(command))
    elif directory in {"vm_ssh_http", "suanli_vm_ssh_http"}:
        command = tmp_path / "ssh"
        command.write_text('#!/bin/sh\nfor arg do last="$arg"; done\ncase "$last" in false) exit 1;; *) echo executed;; esac\n')
        command.chmod(0o755)
        if directory == "vm_ssh_http":
            (credentials / "id_ed25519").write_text("fixture-key")
            gateway = module.VmSshGateway(instance_path=instance, state_root=state, credential_root=credentials, ssh=str(command))
        else:
            (state / ".ssh").mkdir()
            (state / ".ssh/id_ecdsa").write_text("fixture-key")
            gateway = module.VmSshGateway(instance_path=instance, state_root=state, ssh=str(command))
    else:
        if directory == "outline_http":
            (credentials / "outline_key").write_text("fixture-key")
            gateway = module.OutlineGateway(credential_root=credentials)
        else:
            (credentials / "AccessKeyId").write_text("fixture-id")
            (credentials / "SecretKey").write_text("fixture-secret")
            gateway = module.SuanliGateway(credential_root=credentials)

        class Response:
            headers = {}
            def __init__(self, value, status):
                self.value, self.status_code = value, status
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def iter_bytes(self):
                yield json.dumps(self.value).encode()

        def provider(method, url, **kwargs):
            data = json.loads(kwargs["content"])
            external["calls"].append((url, data))
            if external["failed"]:
                return Response({"Error": {"Code": "Unavailable"}}, 503)
            if external["invalid"]:
                return Response([], 200)
            if directory == "outline_http":
                return Response({"ok": True, "data": {"id": "fixture-document"}}, 200)
            action = data["Action"]
            key = {"DescribeRegions": "Regions", "DescribeZones": "Zones", "DescribeImages": "Images", "DescribeNodes": "Nodes", "DescribeInstances": "Instances", "RunInstances": "Instances", "DescribeInstanceStatus": "InstanceStatuses"}.get(action, "StateTransitions")
            return Response({key: []}, 200)
        monkeypatch.setattr(module.httpx, "stream", provider)
    handler = type("FixtureHandler", (module.Handler,), {"gateway": gateway})
    return gateway, handler, external


def arguments(directory, action_name):
    common = {"name": "fixture-job", "pod_name": "fixture-pod", "resource": "pods", "id": "fixture-id", "documentId": "fixture-document", "collectionId": "fixture-collection", "title": "Fixture", "query": "fixture", "command": "id", "RegionId": "region-test", "ZoneId": "zone-test", "InstanceIds": ["vm-test"], "Filters": [], "IncludeAllInstances": True}
    schema = json.loads((ROOT / "capability_services" / directory / "schemas" / f"{action_name}-input.json").read_text())
    value = {key: common[key] for key in schema.get("required", []) if key in common}
    if action_name == "jobs-submit":
        value = {"manifest": {"apiVersion": "batch.volcano.sh/v1alpha1", "kind": "Job", "metadata": {"name": "fixture-job", "namespace": "test"}, "spec": {}}}
    if action_name == "instances-run":
        value = {"ProjectId": "project-test", "RegionId": "region-test", "ZoneId": "zone-test", "Count": 1, "Cpu": 2, "Mem": 4, "DiskSize": 20, "ImageId": "image-test", "Name": "fixture-vm", "TagSpecifications": [], "ClientToken": "fixture-create"}
    return value


@pytest.mark.parametrize("directory", PACKAGES)
async def test_real_service_package_grip_lifecycle_and_every_export(directory, tmp_path, monkeypatch):
    gateway, handler, external = gateway_fixture(directory, tmp_path, monkeypatch)
    settings = Settings(grip_token="service-test", public_base_url="http://observer", coding_agent_backend="none", agent_lease_ttl_seconds=300)
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services" / directory)
    with serve(handler) as port:
        runner = DockerFixture()
        transport = LocalGatewayTransport(port)
        profile = bundle.manifest["body"].get("credential_profile")
        if profile:
            shutil.copytree(tmp_path / "credentials", tmp_path / profile)
        runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", egress_network="egress", runner=runner, http_transport=transport, credential_dir=tmp_path, instance_state_dir=tmp_path / "instances")
        app.state.slaves["slave-a"].runtime_plugin_host = RuntimePluginHost(plugins={('service', 'container:http', '1'): RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo, app.state.slaves)
        try:
            async with LoomClient("http://observer", token=settings.grip_token, transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
                dependencies = {}
                if directory == "suanlicloud_http":
                    dependencies["grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v2"] = await ServicePackage(ROOT / "capability_services/suanli_vm_ssh_http").publish(client, image_ref=IMAGE)
                package = await bundle.publish(client, image_ref=IMAGE, dependencies=dependencies)
                raw = await client.invoke(package, client.names.action("read"), {})
                assert raw["publication_state"] == "published"
                exports = (await client.describe(package))["resource"]["metadata"]["actions"]
                assert len(exports) == len(bundle.manifest["action_exports"])
                slave = next(r["uri"] for r in await client.resources(kind="slave") if r["metadata"]["slave_id"] == "slave-a")
                config = {"host": "192.0.2.7", "port": 22, "user": "root"} if "ssh" in directory else {}
                activation = await client.invoke(slave, client.names.action("activate-package"), {"package": package, "instance_name": "integration", "instance_config": config, "idempotency_key": "activate"})
                target = activation["resource"]
                auth = await client.invoke(target, client.names.action("auth-start"), {"expected_revision": activation["revision"]})
                if directory == "suanli_vm_ssh_http":
                    assert activation["state"] == "pending_auth"
                    assert auth["browser_auth"][0]["client"]["bridge_url"].endswith("/api/v1/auth/browser/bridge")
                    pending = await client.describe(target)
                    assert not any("/exports/" in o["action"]["uri"] for o in pending["offers"])
                    assert len(pending["resource"]["metadata"]["actions"]) == len(exports)
                    assert all(not a["available"] and a["unavailable_reason"] == "activation_pending_auth"
                               for a in pending["resource"]["metadata"]["actions"])
                    with pytest.raises(GripError, match="unsupported_action"):
                        await client.invoke(target, exports[0]["uri"], {})
                    # Simulate the external browser client finishing and installing
                    # its key; the real runtime probes SSH and emits health reports.
                    gateway.close()
                    worker = app.state.slaves["slave-a"]
                    for _ in range(2):
                        for report in await worker.inspect_activation_health():
                            await repo.record_capability_health(report)
                            worker.acknowledge_health_report(report.report_id)
                    assert (await client.describe(target))["resource"]["metadata"]["state"] == "ready"
                else:
                    assert activation["state"] == "ready", activation
                    assert auth["health_reports"][0]["activation_state"] == "ready", auth
                description = await client.describe(target)
                metadata = description["resource"]["metadata"]
                assert metadata["package"] == package
                assert metadata["host"] == slave
                assert metadata["instance_name"] == "integration"
                assert metadata["description"]
                declarations = metadata["actions"]
                assert len(declarations) == len(exports)
                if directory == "suanlicloud_http":
                    assert len(declarations) == 12
                offers = description["offers"]
                assert len([offer for offer in offers if "/exports/" in offer["action"]["uri"]]) == len(exports)
                for declaration, published in zip(declarations, raw["action_exports"], strict=True):
                    assert declaration["available"] is True
                    assert declaration["unavailable_reason"] is None
                    assert declaration["name"] and declaration["description"]
                    assert declaration["descriptor"]["schema_version"] == "action.v1"
                    assert declaration["uri"] in [o["action"]["uri"] for o in offers]
                    assert declaration["action_descriptor_ref"]["resource_id"] == published["action_descriptor_ref"]["resource_id"]
                    contract = await client.read_json(ResourceRef.model_validate(declaration["io_contract_ref"]))
                    for key, contract_key in (("arguments_schema", "input_schema_ref"), ("result_schema", "output_schema_ref")):
                        assert declaration[key] == contract[contract_key]
                        assert (await client.read_json(ResourceRef.model_validate(declaration[key])))["type"] == "object"
                for source, action in zip(bundle.manifest["action_exports"], exports, strict=True):
                    action_name = source["action_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
                    data = {"arguments": arguments(directory, action_name), "expected_revision": activation["revision"], "idempotency_key": action_name}
                    submitted = await client.invoke(target, action["uri"], data)
                    assert await client.invoke(target, action["uri"], data) == submitted
                    result = await wait_result(app, client, submitted["run"])
                    assert result["state"] == "completed", (action_name, result)
                business_paths = {e["runtime_binding"]["path"] for e in bundle.manifest["action_exports"]}
                calls = [p for _, p, _ in transport.calls if p in business_paths]
                assert sorted(calls) == sorted(business_paths), "each export runs once despite repeated GRIP invocation"
                if "ssh" in directory:
                    call = await client.invoke(target, exports[0]["uri"], {"arguments": {"command": "false"}, "expected_revision": activation["revision"], "idempotency_key": "failed-command"})
                    failure_result = await wait_result(app, client, call["run"])
                    assert failure_result["state"] in {"failed", "awaiting_decision"}, failure_result
                    with pytest.raises(GripError, match="invalid_input"):
                        await client.invoke(target, exports[0]["uri"], {"arguments": {"command": "id", "timeout_seconds": -1}, "expected_revision": activation["revision"], "idempotency_key": "invalid-input"})
                elif directory in {"outline_http", "suanlicloud_http"}:
                    for failure in ("failed", "invalid"):
                        external[failure] = True
                        call = await client.invoke(target, exports[0]["uri"], {"arguments": {}, "expected_revision": activation["revision"], "idempotency_key": failure})
                        failure_result = await wait_result(app, client, call["run"])
                        assert failure_result["state"] in {"failed", "awaiting_decision"}, failure_result
                        external[failure] = False
                # A second instance has its own Docker identity and can be stopped
                # independently even though it shares the immutable package.
                second_config = {**config, "host": "192.0.2.8"} if config else {}
                second = await client.invoke(slave, client.names.action("activate-package"), {
                    "package": package, "instance_name": "second", "instance_config": second_config,
                    "idempotency_key": "activate-second",
                })
                assert second["resource"] != target
                assert {c["Config"]["Labels"]["io.loom.instance-name"] for c in runner.containers.values()} == {"integration", "second"}
                if config:
                    configurations = [json.loads(path.read_text()) for path in (tmp_path / "instances").rglob("*.json")]
                    assert {c["host"] for c in configurations} == {"192.0.2.7", "192.0.2.8"}
                stopped = await client.invoke(target, client.names.action("stop"), {"expected_revision": activation["revision"], "idempotency_key": "stop"})
                assert stopped["state"] == "stopped"
                stopped_description = await client.describe(target)
                stopped_actions = stopped_description["resource"]["metadata"]["actions"]
                assert [a["uri"] for a in stopped_actions] == [a["uri"] for a in declarations]
                assert all(not a["available"] and a["unavailable_reason"] == "desired_state_stopped" for a in stopped_actions)
                assert all("/exports/" not in o["action"]["uri"] for o in stopped_description["offers"])
                with pytest.raises(GripError, match="unsupported_action"):
                    await client.invoke(target, exports[0]["uri"], {})
                remaining, = runner.containers.values()
                assert remaining["Config"]["Labels"]["io.loom.instance-name"] == "second"
                await client.invoke(second["resource"], client.names.action("stop"), {"expected_revision": second["revision"], "idempotency_key": "stop-second"})
                assert not runner.containers
                assert not any("fixture-secret" in json.dumps(item) for item in await client.resources())
        finally:
            await app.state.driver.stop_execution_loop()
            for slave in app.state.slaves.values():
                await slave.close()
            if hasattr(gateway, "close"):
                gateway.close()


async def test_service_publisher_rejects_mutable_image_and_unresolved_access_package():
    from loom_v2.observer.app import create_app
    settings = Settings(grip_token="publisher-test", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    app = create_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services/suanlicloud_http")
    async with LoomClient("http://observer", token=settings.grip_token, transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
        with pytest.raises(ValueError, match="immutable_image_ref_required"):
            await bundle.publish(client, image_ref="registry.example/cloud:latest")
        with pytest.raises(ValueError, match="service_artifact_dependency_missing:grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v2"):
            await bundle.publish(client, image_ref=IMAGE)
        assert await client.resources(kind="capability-package") == []
