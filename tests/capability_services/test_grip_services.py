"""Real manifests -> GRIP -> Driver/Slave -> container:http -> real HTTP gateways.

Only Docker and external providers are fixtures; no live infrastructure is used.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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
PACKAGES = ("kubernetes_http", "volcano_http", "grafana_http", "outline_http", "suanlicloud_http", "vm_ssh_http", "suanli_vm_ssh_http")
IMAGE = "registry.example/loom/test@sha256:" + "a" * 64


async def wait_result(app, client, run):
    app.state.driver.start_execution_loop()
    for _ in range(100):
        result = await client.invoke(run, client.names.action("read"), {})
        if result["state"] in {"completed", "failed", "cancelled", "awaiting_decision"}:
            return result
        await asyncio.sleep(.02)
    pytest.fail(f"execution did not settle: {result}")


@asynccontextmanager
async def serve(handler):
    from fastapi import FastAPI
    from tests.support.asgi_server import running_asgi
    if isinstance(handler, FastAPI):
        async with running_asgi(handler) as base:
            yield int(base.rsplit(":", 1)[1])
        return
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

    async def open_stream(self, package, export, payload, *, activation, request_id, deadline=None):
        from loom_orchestration.streams import connect_stream, STREAM_PROTOCOL
        await self.runtime._invocation_endpoint(package.model_dump(mode="json"), export.model_dump(mode="json"), activation)
        request = {"version":"grip.v1", "request_id":request_id, "method":"INVOKE", "auth":{"credential":"runtime-local"},
                   "target":package.version_ref,"action":export.action_descriptor_ref.resource_id,"input":payload,"accepted_protocols":[STREAM_PROTOCOL]}
        return await connect_stream(f"ws://127.0.0.1:{self.runtime.http_transport.port}" + export.runtime_binding["path"], request)

    async def auth_start(self, package, activation):
        return await self.runtime.auth_start(package.model_dump(mode="json"), activation)

    async def auth_browser(self, package, activation, payload):
        return await self.runtime.auth_browser(package.model_dump(mode="json"), activation, payload)

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
    elif directory == "volcano_http":
        instance.write_text(json.dumps({"api_origin": "https://volcano-internal.example"}))
        def provider(request):
            external["calls"].append((str(request.url), request.method))
            if external["failed"]:
                return httpx.Response(503)
            if external["invalid"]:
                return httpx.Response(200, json=[])
            item = {"metadata": {"name": "fixture", "namespace": "test"}}
            if request.url.path == "/api/pods":
                item.update(spec={"nodeName": "node-a", "containers": [{"resources": {"limits": {"nvidia.com/gpu": "2"}}}]}, status={"phase": "Running"})
            return httpx.Response(200, json={"items": [item], "totalCount": 1})
        gateway = module.VolcanoGateway(instance_path=instance, transport=httpx.MockTransport(provider))
    elif directory == "grafana_http":
        instance.write_text(json.dumps({"api_origin": "https://grafana-internal.example", "gpu_metric": {
            "name": "DCGM_FI_DEV_GPU_UTIL", "node_label": "Hostname", "index_label": "gpu", "unit": "percent"}}))
        (credentials / "grafana_token").write_text("fixture-secret")
        def provider(request):
            external["calls"].append((str(request.url), request.method))
            assert request.headers["Authorization"] == "Bearer fixture-secret"
            if external["failed"]:
                return httpx.Response(503)
            if external["invalid"]:
                return httpx.Response(200, json={"invalid": True})
            if request.url.path == "/api/datasources":
                return httpx.Response(200, json=[{"uid": "prom", "name": "Prometheus", "type": "prometheus", "isDefault": True},
                                                {"uid": "prometheus", "name": "Arena metrics", "type": "prometheus"}])
            if request.url.path == "/api/search":
                return httpx.Response(200, json=[{"uid": "gpu", "title": "GPU"}])
            if request.url.path == "/api/dashboards/uid/gpu":
                return httpx.Response(200, json={"dashboard": {"uid": "gpu", "title": "GPU", "panels": []}})
            if request.url.path == "/api/dashboards/uid/gpu-arena":
                return httpx.Response(200, json={"dashboard": json.loads((ROOT / "tests/fixtures/gpu-arena-dashboard.json").read_text())})
            range_query = request.url.path.endswith("/query_range")
            data = {"metric": {"Hostname": "node-a", "gpu": "0", "UUID": "GPU-fixture"},
                    "values" if range_query else "value": [[10, "42"], [20, "50"]] if range_query else [10, "42"]}
            if request.url.params.get("query", "").startswith("sum_over_time"):
                data = {"metric": {"UUID": "GPU-fixture", "exported_namespace": "test"}, "value": [20, "2"]}
            elif request.url.params.get("query", "").startswith("round(quantile_over_time"):
                data = {"metric": {"UUID": "GPU-fixture"}, "value": [20, "40"]}
            return httpx.Response(200, json={"status": "success", "data": {
                "resultType": "matrix" if range_query else "vector", "result": [data]}})
        gateway = module.GrafanaGateway(instance_path=instance, credential_root=credentials,
                                       state_root=state, transport=httpx.MockTransport(provider))
    elif directory in {"vm_ssh_http", "suanli_vm_ssh_http"}:
        command = tmp_path / "ssh"
        command.write_text('#!/bin/sh\nexec /bin/sh\n')
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
    if directory == "kubernetes_http":
        from capability_services.kubernetes_http.service_access import ServiceAccess, OWNER
        service_ref="grip://loom.local/workspaces/workspace-default/resources/services/fixture"
        access=ServiceAccess(gateway, {"service_targets":{service_ref:{"namespace":"test", "name":"fixture", "port":8001,
            "selector":{"app":"fixture"}, "workload":{"resource":"deployment","name":"fixture","uid":"workload"}}}})
        original_run=gateway._run
        def command(args, **kwargs):
            if "deployment" in args:
                value={"metadata":{"uid":"workload"},"spec":{"template":{"metadata":{"labels":{"app":"fixture"}}}}}
            elif "service" in args:
                value={"metadata":{"uid":"service","name":"fixture","namespace":"test","annotations":{OWNER:service_ref},
                                   "ownerReferences":[ServiceAccess.owner_reference(access.targets[service_ref])]},
                       "spec":{"type":"ClusterIP","clusterIP":"10.0.0.1","selector":{"app":"fixture"},
                               "ports":[{"name":"http","port":8001,"targetPort":8001,"protocol":"TCP"}]}}
            else:return original_run(args, **kwargs)
            return 0,json.dumps(value),"",False
        monkeypatch.setattr(gateway,"_run",command)
        def start(ref):
            access.channels[ref]={"state":"ready","port":external["local_port"],"streams":set(),"task":None}
        access.start=start
        return gateway, module.create_app(gateway,access), external
    handler = type("FixtureHandler", (module.Handler,), {"gateway": gateway})
    return gateway, handler, external


def arguments(directory, action_name):
    common = {"service_ref":"grip://loom.local/workspaces/workspace-default/resources/services/fixture", "name": "fixture-job", "pod_name": "fixture-pod", "resource": "pods", "id": "fixture-id", "documentId": "fixture-document", "collectionId": "fixture-collection", "title": "Fixture", "query": "fixture", "command": "id", "RegionId": "region-test", "ZoneId": "zone-test", "InstanceIds": ["vm-test"], "Filters": [], "IncludeAllInstances": True, "uid": "gpu", "hostname": "node-a", "start": 10, "end": 20, "step": 10}
    schema = json.loads((ROOT / "capability_services" / directory / "schemas" / f"{action_name}-input.json").read_text())
    value = {key: common[key] for key in schema.get("required", []) if key in common}
    if action_name == "jobs-submit":
        value = {"manifest": {"apiVersion": "batch.volcano.sh/v1alpha1", "kind": "Job", "metadata": {"name": "fixture-job", "namespace": "test"}, "spec": {}}}
    if action_name == "instances-run":
        value = {"ProjectId": "project-test", "RegionId": "region-test", "ZoneId": "zone-test", "Count": 1, "Cpu": 2, "Mem": 4, "DiskSize": 20, "ImageId": "image-test", "Name": "fixture-vm", "TagSpecifications": [], "ClientToken": "fixture-create"}
    if action_name == "gpu-arena-query":
        value = {"panel_id": 1, "ref_id": "A", "start": 10, "end": 20, "step": 10}
    if action_name == "gpu-arena-gpu-hours":
        value = {"start": 10, "end": 20, "step": 10}
    return value


@pytest.mark.parametrize("directory", PACKAGES)
async def test_real_service_package_grip_lifecycle_and_every_export(directory, tmp_path, monkeypatch):
    gateway, handler, external = gateway_fixture(directory, tmp_path, monkeypatch)
    settings = Settings(grip_token="service-test", public_base_url="http://observer", coding_agent_backend="none", agent_lease_ttl_seconds=300)
    if directory == "kubernetes_http":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
        key=Ed25519PrivateKey.generate()
        (tmp_path/"signing.pem").write_bytes(key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()))
        (tmp_path/"verify.pem").write_bytes(key.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo))
        settings.grip_signing_key_file=str(tmp_path/"signing.pem")
        settings.grip_verification_keys={settings.grip_signing_key_id:str(tmp_path/"verify.pem")}
        settings.grip_allow_insecure_services=True
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services" / directory)
    async with serve(handler) as port:
        external["local_port"]=port
        runner = DockerFixture()
        transport = LocalGatewayTransport(port)
        profile = "service-test" if directory not in {"suanli_vm_ssh_http", "volcano_http"} else None
        if profile:
            shutil.copytree(tmp_path / "credentials", tmp_path / profile)
        runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", egress_network="egress", runner=runner, http_transport=transport, credential_dir=tmp_path, instance_state_dir=tmp_path / "instances")
        app.state.slaves["slave-a"].runtime_plugin_host = RuntimePluginHost(plugins={('service', 'container:http', '1'): RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo, app.state.slaves)
        try:
            async with LoomClient("http://observer", token=settings.grip_token, transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
                dependencies = {}
                if directory == "suanlicloud_http":
                    dependencies["grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v5"] = await ServicePackage(ROOT / "capability_services/suanli_vm_ssh_http").publish(client, image_ref=IMAGE)
                package = await bundle.publish(client, image_ref=IMAGE, dependencies=dependencies)
                raw = await client.invoke(package, client.names.action("read"), {})
                assert raw["publication_state"] == "published"
                exports = (await client.describe(package))["resource"]["metadata"]["actions"]
                assert len(exports) == len(bundle.manifest["action_exports"])
                slave = next(r["uri"] for r in await client.resources(mcue="computing/slave") if r["metadata"]["slave_id"] == "slave-a")
                config = {"host": "192.0.2.7", "port": 22, "user": "root"} if "ssh" in directory else {}
                binding = {}
                if directory == "volcano_http":
                    config = {"api_origin": "https://volcano-internal.example"}
                    volcano_uri = client.names.name("resources", "volcano", "beijing")
                    related_kubernetes = client.names.name("activations", "kubernetes-http", "v4", "slave-a", "beijing")
                    await client.invoke(client.names.root, client.names.action("register-resource"), {
                        "resource_id": volcano_uri, "mcue_ref": client.names.mcue("computing/volcano"), "package": package,
                        "attributes": {"dashboard_url": "https://volcano.example:7443", "kubernetes_ref": related_kubernetes},
                    })
                    binding = {"target": volcano_uri}
                elif directory == "grafana_http":
                    config = {"api_origin": "https://grafana-internal.example", "gpu_metric": {
                        "name": "DCGM_FI_DEV_GPU_UTIL", "node_label": "Hostname", "index_label": "gpu", "unit": "percent"}}
                    grafana_uri = client.names.name("resources", "grafana", "bj")
                    related_kubernetes = client.names.name("activations", "kubernetes-http", "v4", "slave-a", "beijing")
                    await client.invoke(client.names.root, client.names.action("register-resource"), {
                        "resource_id": grafana_uri, "mcue_ref": client.names.mcue("computing/grafana"), "package": package,
                        "attributes": {"dashboard_url": "https://grafana.example:7443", "kubernetes_ref": related_kubernetes},
                    })
                    binding = {"target": grafana_uri}
                activation = await client.invoke(slave, client.names.action("activate-package"), {"package": package, "instance_name": "integration", "instance_config": config, "credential_profile": profile, "idempotency_key": "activate", **binding})
                target = activation["resource"]
                stored = next(a for a in app.state.slaves["slave-a"].activations.values() if a.instance_name == "integration")
                assert stored.credential_profile == profile
                assert "credential_profile" not in json.dumps(await client.describe(target))
                mounts = [args for args in runner.calls if args[0] == "create"]
                if profile:
                    assert any(f"src={tmp_path / profile},dst=/run/loom/credentials,readonly" in arg for args in mounts for arg in args)
                else:
                    assert not any("dst=/run/loom/credentials" in arg for args in mounts for arg in args)
                auth = (await client.invoke(target, client.names.action("auth-start"), {"expected_revision": activation["revision"]})
                        if bundle.manifest["body"].get("authentication") and directory != "grafana_http" else None)
                if directory == "suanli_vm_ssh_http":
                    assert activation["state"] == "pending_auth"
                    assert auth["authentication"]["client"]["bridge_url"].endswith("/api/v1/auth/browser/bridge")
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
                    gateway.private_key.write_text("fixture-key-after-login")
                    worker = app.state.slaves["slave-a"]
                    for _ in range(2):
                        for report in await worker.inspect_activation_health():
                            await repo.record_capability_health(report)
                            worker.acknowledge_health_report(report.report_id)
                    assert (await client.describe(target))["resource"]["metadata"]["state"] == "ready"
                else:
                    assert activation["state"] == "ready", activation
                    if auth is not None:
                        assert auth["authentication"]["status"] == "authenticated", auth
                description = await client.describe(target)
                metadata = description["resource"]["metadata"]
                if bundle.manifest["body"].get("authentication"):
                    assert metadata["authentication"]["status"] == "authenticated"
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
                    interaction=source["runtime_binding"].get("interaction","task")
                    if interaction == "request-reply":
                        result=await client.invoke(target,action["uri"],arguments(directory,action_name))
                        assert result["state"] == "ready"
                        continue
                    if interaction == "byte-stream":
                        from loom_orchestration.grip import Request
                        _, payload, _, _ = await client._prepare(target,action["uri"],arguments(directory,action_name),mode="byte-stream")
                        opened=await app.state.service_access.open_stream(Request.model_validate(payload))
                        try:
                            await opened.stream.write(b"GET /healthz HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n")
                            assert b"200 OK" in await opened.stream.read(65536)
                        finally:await opened.stream.aclose()
                        continue
                    data = {"arguments": arguments(directory, action_name), "expected_revision": activation["revision"], "idempotency_key": action_name}
                    submitted = await client.invoke(target, action["uri"], data)
                    assert await client.invoke(target, action["uri"], data) == submitted
                    result = await wait_result(app, client, submitted["run"])
                    assert result["state"] == "completed", (action_name, result)
                    if action_name == "gpu-arena-gpu-hours":
                        row, = result["outcome"]["value"]["rows"]
                        assert row["namespace"] == "test" and row["rank"] == 1
                        assert row["gpu_hours"] == 2 and row["a100_80gb_hours"] == 1
                business_paths = {e["runtime_binding"]["path"] for e in bundle.manifest["action_exports"] if e["runtime_binding"].get("interaction") != "byte-stream"}
                calls = [p for _, p, _ in transport.calls if p in business_paths]
                assert sorted(calls) == sorted(business_paths), "each export runs once despite repeated GRIP invocation"
                if "ssh" in directory:
                    long_script = "#" + "x" * (200 * 1024) + '\nprintf "large script ok"'
                    call = await client.invoke(target, exports[0]["uri"], {"arguments": {"command": long_script, "timeout_seconds": 600}, "expected_revision": activation["revision"], "idempotency_key": "long-script"})
                    long_result = await wait_result(app, client, call["run"])
                    assert long_result["state"] == "completed", long_result
                    assert long_result["outcome"]["value"]["stdout"] == "large script ok"
                    for invalid in ({"command": "id", "timeout_seconds": 3601}, {"command": "x" * (1048576 + 1)}):
                        with pytest.raises(GripError, match="invalid_input"):
                            await client.invoke(target, exports[0]["uri"], {"arguments": invalid, "expected_revision": activation["revision"], "idempotency_key": "above-exec-limit"})
                    call = await client.invoke(target, exports[0]["uri"], {"arguments": {"command": "false"}, "expected_revision": activation["revision"], "idempotency_key": "failed-command"})
                    failure_result = await wait_result(app, client, call["run"])
                    assert failure_result["state"] in {"failed", "awaiting_decision"}, failure_result
                    with pytest.raises(GripError, match="invalid_input"):
                        await client.invoke(target, exports[0]["uri"], {"arguments": {"command": "id", "timeout_seconds": -1}, "expected_revision": activation["revision"], "idempotency_key": "invalid-input"})
                elif directory in {"outline_http", "suanlicloud_http", "volcano_http", "grafana_http"}:
                    for failure in ("failed", "invalid"):
                        external[failure] = True
                        call = await client.invoke(target, exports[0]["uri"], {"arguments": {}, "expected_revision": activation["revision"], "idempotency_key": failure})
                        failure_result = await wait_result(app, client, call["run"])
                        assert failure_result["state"] in {"failed", "awaiting_decision"}, failure_result
                        external[failure] = False
                if directory == "volcano_http":
                    domain = await client.describe(volcano_uri)
                    assert domain["resource"]["metadata"]["attributes"]["kubernetes_ref"] == related_kubernetes
                    assert domain["resource"]["metadata"]["attributes"]["dashboard_url"] == "https://volcano.example:7443"
                    assert config["api_origin"] != domain["resource"]["metadata"]["attributes"]["dashboard_url"]
                    assert domain["resource"]["metadata"]["activations"] == [target]
                    assert metadata["target"] == volcano_uri
                    gpu_action = next(a["uri"] for a in declarations if a["name"] == "gpu_usage")
                    direct = await client.invoke(volcano_uri, gpu_action, {
                        "arguments": {}, "expected_revision": activation["revision"], "idempotency_key": "direct-volcano-gpu",
                    })
                    direct_result = await wait_result(app, client, direct["run"])
                    assert direct_result["state"] == "completed"
                    assert direct_result["outcome"]["value"]["allocated"] == 2
                if directory == "grafana_http":
                    domain = await client.describe(grafana_uri)
                    assert domain["resource"]["mcue"] == "computing/grafana"
                    assert domain["resource"]["metadata"]["attributes"]["kubernetes_ref"] == related_kubernetes
                    assert domain["resource"]["metadata"]["activations"] == [target]
                    assert metadata["target"] == grafana_uri
                    gpu_action = next(a["uri"] for a in declarations if a["name"] == "gpu_utilization")
                    direct = await client.invoke(grafana_uri, gpu_action, {
                        "arguments": {"hostname": "node-a"}, "expected_revision": activation["revision"],
                        "idempotency_key": "direct-grafana-gpu",
                    })
                    direct_result = await wait_result(app, client, direct["run"])
                    assert direct_result["state"] == "completed", direct_result
                    point, = direct_result["outcome"]["value"]["samples"]
                    assert (point["hostname"], point["gpu_index"], point["utilization_percent"]) == ("node-a", 0, 42)
                # A second instance has its own Docker identity and can be stopped
                # independently even though it shares the immutable package.
                second_config = {**config, "host": "192.0.2.8"} if config else {}
                if directory == "volcano_http":
                    second_config = {"api_origin": "https://second-volcano-internal.example"}
                elif directory == "grafana_http":
                    second_config = {**config, "api_origin": "https://second-grafana-internal.example"}
                second = await client.invoke(slave, client.names.action("activate-package"), {
                    "package": package, "instance_name": "second", "instance_config": second_config,
                    "idempotency_key": "activate-second",
                })
                assert second["resource"] != target
                assert {c["Config"]["Labels"]["io.loom.instance-name"] for c in runner.containers.values()} == {"integration", "second"}
                if "host" in config:
                    configurations = [json.loads(path.read_text()) for path in (tmp_path / "instances").rglob("*.json")]
                    assert {c["host"] for c in configurations} == {"192.0.2.7", "192.0.2.8"}
                elif directory in {"volcano_http", "grafana_http"}:
                    configurations = [json.loads(path.read_text()) for path in (tmp_path / "instances").rglob("*.json")]
                    assert {c["api_origin"] for c in configurations} == {config["api_origin"], second_config["api_origin"]}
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
        with pytest.raises(ValueError, match="service_artifact_dependency_missing:grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v5"):
            await bundle.publish(client, image_ref=IMAGE)
        assert await client.resources(mcue="capability/package") == []


@pytest.mark.parametrize("rotate_session", [False, True])
async def test_grafana_browser_auth_resumes_the_same_instance_and_keeps_cookie_out_of_runs(tmp_path, rotate_session):
    from capability_services.grafana_http.server import GrafanaGateway, Handler
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"api_origin": "https://grafana-internal.example",
                                   "browser_origin": "https://grafana-public.example:7443"}))
    authorized = {"allow": True}
    session = {"cookie": "browser-private-cookie", "rotation_required": False, "rotations": 0}
    def provider(request):
        if not authorized["allow"] or request.headers.get("Cookie") != "grafana_session=" + session["cookie"]:
            return httpx.Response(401)
        if request.method == "POST" and request.url.path == "/api/user/auth-tokens/rotate":
            session.update(cookie="browser-rotated-cookie", rotation_required=False, rotations=session["rotations"] + 1)
            return httpx.Response(200, json={}, headers={"Set-Cookie": "grafana_session=browser-rotated-cookie; Path=/; Secure"})
        if session["rotation_required"]:
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        if request.url.path == "/api/datasources":
            return httpx.Response(200, json=[{"uid": "prom", "name": "Prometheus", "type": "prometheus", "isDefault": True}])
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"Hostname": "node-a", "gpu": "2"}, "value": [10, "37"]}]}})
    gateway = GrafanaGateway(instance_path=instance, credential_root=tmp_path / "credentials",
                             state_root=tmp_path / "state", transport=httpx.MockTransport(provider))
    handler = type("BrowserGrafanaHandler", (Handler,), {"gateway": gateway})
    settings = Settings(grip_token="browser-test", public_base_url="http://observer", coding_agent_backend="none", agent_lease_ttl_seconds=300)
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services/grafana_http")
    async with serve(handler) as port:
        runner = DockerFixture()
        runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", egress_network="egress",
            runner=runner, http_transport=LocalGatewayTransport(port), credential_dir=tmp_path,
            instance_state_dir=tmp_path / "instances")
        app.state.slaves["slave-a"].runtime_plugin_host = RuntimePluginHost(
            plugins={('service', 'container:http', '1'): RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo, app.state.slaves)
        try:
            async with LoomClient("http://observer", token=settings.grip_token,
                    transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
                package = await bundle.publish(client, image_ref=IMAGE)
                slave = (await client.resources(mcue="computing/slave"))[0]["uri"]
                activated = await client.invoke(slave, client.names.action("activate-package"), {
                    "package": package, "instance_name": "bj", "instance_config": json.loads(instance.read_text()),
                    "idempotency_key": "activate-browser-grafana"})
                assert activated["state"] == "pending_auth"
                target = activated["resource"]
                desc = await client.describe(target)
                assert all(not x["available"] for x in desc["resource"]["metadata"]["actions"])
                stored = next(iter(app.state.slaves["slave-a"].activations.values()))
                for attempt in range(3):
                    started = await client.invoke(target, client.names.action("auth-start"), {"expected_revision": activated["revision"]})
                    login = started["authentication"]
                    info = login["client"]
                    replaced = await client.invoke(target, client.names.action("auth-start"), {"expected_revision": activated["revision"]})
                    old_info, info = info, replaced["authentication"]["client"]
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://observer") as browser:
                        stale = await browser.post("/api/v1/auth/browser/bridge",
                            headers={"Authorization": "Bearer " + old_info["session_token"]},
                            json={"action": "open", "session_id": old_info["session_id"]})
                        assert stale.status_code == 404
                        headers = {"Authorization": "Bearer " + info["session_token"]}
                        opened = await browser.post("/api/v1/auth/browser/bridge", headers=headers,
                            json={"action": "open", "session_id": info["session_id"], "query": {}})
                        assert opened.status_code == 200, opened.text
                        value = opened.json()
                        assert value["location"] == "https://grafana-public.example:7443/login/generic_oauth"
                        callback = {"action": "callback", "session_id": info["session_id"],
                                    "browser_binding": value["browser_binding"],
                                    "query": {"grafana_session": ["browser-private-cookie"]}}
                        accepted = await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)
                        assert accepted.status_code == 200 and accepted.json() == {"status": "accepted"}
                        assert (await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)).status_code == 404
                    worker = app.state.slaves["slave-a"]
                    for _ in range(2):
                        for report in await worker.inspect_activation_health():
                            await repo.record_capability_health(report)
                            worker.acknowledge_health_report(report.report_id)
                    ready = await client.describe(target)
                    assert ready["resource"]["metadata"]["state"] == "ready"
                    assert ready["resource"]["metadata"]["revision"] == activated["revision"]
                    assert ready["resource"]["metadata"]["authentication"]["status"] == "authenticated"
                    assert info["session_token"] not in json.dumps(ready)
                    if attempt < 2:
                        authorized["allow"] = False
                        for report in await worker.inspect_activation_health():
                            await repo.record_capability_health(report)
                            worker.acknowledge_health_report(report.report_id)
                        expired = (await client.describe(target))["resource"]["metadata"]
                        assert expired["state"] == "pending_auth"
                        assert expired["authentication"]["status"] == "auth_required"
                        if attempt == 1:
                            from loom_v2.contracts.types import CapabilityHealthReport
                            await repo.record_capability_health(CapabilityHealthReport(
                                report_id="historical-auth-failure", package_version_ref=stored.package_version_ref,
                                package_digest=stored.package_digest, target_slave="slave-a", instance_name="bj",
                                activation_state="failed", activation_revision=activated["revision"]))
                            key = next(iter(worker.activations))
                            worker.activations[key] = worker.activations[key].model_copy(update={"activation_state": "failed"})
                        authorized["allow"] = True
                session["rotation_required"] = rotate_session
                action = next(x for x in ready["resource"]["metadata"]["actions"] if x["name"] == "gpu_utilization")
                submitted = await client.invoke(target, action["uri"], {
                    "arguments": {"hostname": "node-a", "datasource_uid": "prom", "gpu_metric": {
                        "name": "DCGM_FI_DEV_GPU_UTIL", "node_label": "Hostname", "index_label": "gpu", "unit": "percent"}},
                    "expected_revision": activated["revision"], "idempotency_key": "gpu-after-browser-login"})
                outcome = await wait_result(app, client, submitted["run"])
                assert outcome["state"] == "completed", outcome
                assert outcome["outcome"]["value"]["samples"][0]["utilization_percent"] == 37
                assert "browser-private-cookie" not in json.dumps(await client.resources())
                assert "browser-private-cookie" not in json.dumps(outcome)
                assert "browser-rotated-cookie" not in json.dumps(outcome)
                assert json.loads(gateway.state_path.read_text())["cookie"] == session["cookie"]
                assert session["rotations"] == int(rotate_session)
                for report in await worker.inspect_activation_health():
                    await repo.record_capability_health(report)
                    worker.acknowledge_health_report(report.report_id)
                current = (await client.describe(target))["resource"]["metadata"]
                assert current["state"] == "ready"
                assert current["revision"] == activated["revision"]
                assert current["authentication"]["status"] == "authenticated"
                created = [call for call in runner.calls if call[0] == "create"]
                assert len(created) == 1
                assert any("target=/var/lib/loom/service" in str(arg) for arg in created[0])
        finally:
            await app.state.driver.stop_execution_loop()
            for slave in app.state.slaves.values():
                await slave.close()
            gateway.close()
