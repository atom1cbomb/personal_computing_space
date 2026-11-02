"""Publish and exercise Ark through GRIP, Driver, Slave and a real HTTP gateway."""
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import httpx
import pytest

from capability_services.ark_http.server import ArkGateway, Handler
from loom_orchestration import GripError, LoomClient, ServicePackage
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.runtime_plugins import RuntimePluginHost
from loom_v2.testing.observer import create_embedded_app, seed_embedded_slaves
from runtime_plugins.container_http_v1.runtime import DockerContainerHTTPRuntimeV1
from tests.capability_services.test_ark_gateway import ARGUMENTS, KEY, provider
from tests.capability_services.test_grip_services import (
    DockerFixture, IMAGE, LocalGatewayTransport, ROOT, RuntimeAdapter, serve, wait_result,
)


async def test_ark_profile_authentication_all_exports_and_key_expiry(tmp_path):
    authorization = {"valid": True}
    calls = []

    def upstream(request):
        calls.append(request)
        if not authorization["valid"]:
            return httpx.Response(401, text="private key revoked")
        return provider(request)

    credentials = tmp_path / "profiles"
    profile = credentials / "ark-personal"
    profile.mkdir(parents=True)
    # The runtime requires a nonempty profile directory, independently of the
    # package's required credential filename.
    (profile / "profile.txt").write_text("User API key will be provisioned here.")
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"api_mode": "agent-plan"}))
    gateway = ArkGateway(instance_path=instance, credential_root=profile,
                         transport=httpx.MockTransport(upstream))
    handler = type("ArkHandler", (Handler,), {"gateway": gateway})
    key = Ed25519PrivateKey.generate()
    (tmp_path / "sign.pem").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    (tmp_path / "verify.pem").write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(grip_token="ark-integration-test", public_base_url="http://observer",
                        coding_agent_backend="none", agent_lease_ttl_seconds=300,
                        grip_signing_key_file=str(tmp_path / "sign.pem"))
    settings.grip_verification_keys = {settings.grip_signing_key_id: str(tmp_path / "verify.pem")}
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services/ark_http")
    runner = DockerFixture()
    async with serve(handler) as port:
        transport = LocalGatewayTransport(port)
        runtime = DockerContainerHTTPRuntimeV1(
            slave_id="slave-a", network="internal", egress_network="egress", runner=runner,
            http_transport=transport, credential_dir=credentials, instance_state_dir=tmp_path / "instances",
        )
        worker = app.state.slaves["slave-a"]
        worker.runtime_plugin_host = RuntimePluginHost(plugins={('service', 'container:http', '1'): RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo, app.state.slaves)

        async def report_health():
            for report in await worker.inspect_activation_health():
                await repo.record_capability_health(report)
                worker.acknowledge_health_report(report.report_id)

        try:
            async with LoomClient("http://observer", token=settings.grip_token,
                    transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
                package = await bundle.publish(client, image_ref=IMAGE)
                docs = await client.invoke(package, client.names.action("how-to-use"), {})
                assert "documentation" in docs
                slave = next(r["uri"] for r in await client.resources(mcue="computing/slave")
                             if r["metadata"]["slave_id"] == "slave-a")
                checked = await client.invoke(slave, client.names.action("check-package"), {
                    "package": package, "instance_name": "personal", "instance_config": {"api_mode": "agent-plan"}})
                assert checked["ready"], checked
                activated = await client.invoke(slave, client.names.action("activate-package"), {
                    "package": package, "instance_name": "personal", "instance_config": {"api_mode": "agent-plan"},
                    "credential_profile": "ark-personal", "idempotency_key": "activate-ark-personal"})
                assert activated["state"] == "pending_auth"
                target = activated["resource"]
                pending = (await client.describe(target))["resource"]["metadata"]
                assert len(pending["actions"]) == 11
                assert all(not action["available"] for action in pending["actions"])
                assert not calls  # Missing credentials never reach the provider.

                # Reconstruct from the exact JSON that would be mounted by the
                # real container runtime, including its transport metadata.
                mounted_configs = list((tmp_path / "instances").rglob("personal.json"))
                assert len(mounted_configs) == 1
                handler.gateway = ArkGateway(instance_path=mounted_configs[0], credential_root=profile,
                                             transport=httpx.MockTransport(upstream))

                (profile / "api_key").write_text(KEY)
                auth = await client.invoke(target, client.names.action("auth-start"), {
                    "expected_revision": activated["revision"]})
                assert auth["authentication"]["status"] == "authenticated"
                await report_health()
                description = await client.describe(target, refresh=True)
                ready = description["resource"]["metadata"]
                assert ready["state"] == "ready"
                assert len(ready["actions"]) == 11 and all(a["available"] for a in ready["actions"])
                actions = {a["name"]: a for a in ready["actions"]}
                assert "credential_profile" not in json.dumps(description)
                assert KEY not in json.dumps(description)
                created = [call for call in runner.calls if call[0] == "create"]
                assert len(created) == 1
                assert any(f"src={profile},dst=/run/loom/credentials,readonly" in arg for arg in created[0])

                # Every export crosses actual HTTP and its published input/output contracts.
                for export in bundle.manifest["action_exports"]:
                    name = export["action_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
                    declaration = actions[name.replace("-", "_")]
                    assert declaration["description"]
                    if export["runtime_binding"]["interaction"] == "task":
                        payload = {"arguments": ARGUMENTS[name], "expected_revision": ready["revision"],
                                   "idempotency_key": "ark-" + name}
                        submitted = await client.invoke(target, declaration["uri"], payload)
                        assert await client.invoke(target, declaration["uri"], payload) == submitted
                        outcome = await wait_result(app, client, submitted["run"])
                        assert outcome["state"] == "completed", outcome
                        result = outcome["outcome"]["value"]
                    else:
                        result = await client.invoke(target, declaration["uri"], ARGUMENTS[name])
                    assert result["action"] == name.replace("-", "_")
                    assert isinstance(result["result"], dict)
                    assert KEY not in json.dumps(result)
                assert len([r for r in calls if r.method == "POST"]) == 6
                assert len([r for r in calls if r.url.path.endswith("/tasks") and r.method == "POST"]) == 1

                before = len(calls)
                with pytest.raises(GripError, match="invalid_input"):
                    await client.invoke(target, actions["videos_get"]["uri"], {"task_id": "../admin"})
                assert len(calls) == before
                assert KEY not in json.dumps(await client.resources())

                authorization["valid"] = False
                await report_health()
                expired = (await client.describe(target, refresh=True))["resource"]["metadata"]
                assert expired["state"] == "pending_auth"
                assert all(not a["available"] for a in expired["actions"])
                authorization["valid"] = True
                await report_health()
                assert (await client.describe(target, refresh=True))["resource"]["metadata"]["state"] == "ready"
                stopped = await client.invoke(target, client.names.action("stop"), {
                    "expected_revision": ready["revision"], "idempotency_key": "stop-ark"})
                assert stopped["state"] == "stopped"
        finally:
            await app.state.driver.stop_execution_loop()
            for slave_worker in app.state.slaves.values():
                await slave_worker.close()


async def test_ark_http_boundary_rejects_invalid_json_and_private_routes(tmp_path):
    gateway = ArkGateway(instance_path=tmp_path / "instance.json", credential_root=tmp_path)
    handler = type("ArkHandler", (Handler,), {"gateway": gateway})
    async with serve(handler) as port:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert (await client.get("/healthz")).status_code == 200
            for raw in (b"[]", b"not-json", b'{"model":NaN}'):
                reply = await client.post("/v1/chat/completions", content=raw)
                assert reply.status_code == 400 and reply.json() == {"error": "invalid_input"}
            assert (await client.post("/v1/unknown", json={})).status_code == 404
            assert (await client.get("/v1/auth/status")).status_code == 404
            assert (await client.post("/v1/auth/status", json={})).json() == {"status": "auth_required"}
