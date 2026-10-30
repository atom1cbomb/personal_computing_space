"""Publish/activate/login and all five exports through public GRIP HTTP/WS."""
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import httpx

from capability_services.suanli_overleaf_http.client import OverleafClient
from capability_services.suanli_overleaf_http.server import create_app
from loom_orchestration import LoomClient, ServicePackage
from loom_v2.observer.grip import GripService
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.runtime_plugins import RuntimePluginHost
from loom_v2.testing.observer import create_embedded_app, seed_embedded_slaves
from runtime_plugins.container_http_v1.runtime import DockerContainerHTTPRuntimeV1
from tests.capability_services.test_grip_services import DockerFixture, IMAGE, LocalGatewayTransport, ROOT, RuntimeAdapter, serve
from tests.capability_services.test_overleaf_gateway import PROJECT, IMPORTED, gateway, repository  # noqa: F401
from tests.support.asgi_server import running_asgi


async def test_public_package_auth_queries_and_archive_transfers(tmp_path, gateway):
    key = Ed25519PrivateKey.generate()
    private, public = tmp_path / "sign.pem", tmp_path / "verify.pem"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(grip_token="overleaf-test", public_base_url="http://observer",
        coding_agent_backend="none", agent_lease_ttl_seconds=300, grip_allow_insecure_services=True,
        grip_signing_key_file=str(private), runtime_plugin_instance_state_dir=str(tmp_path / "private-state"))
    settings.grip_verification_keys = {settings.grip_signing_key_id: str(public)}
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    async with serve(create_app(gateway)) as port:
        runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", egress_network="egress",
            runner=DockerFixture(), http_transport=LocalGatewayTransport(port), instance_state_dir=tmp_path / "instances")
        worker = app.state.slaves["slave-a"]
        worker.runtime_plugin_host = RuntimePluginHost(plugins={('service', 'container:http', '1'): RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo, app.state.slaves)

        async def health():
            for report in await worker.inspect_activation_health():
                await repo.record_capability_health(report)
                worker.acknowledge_health_report(report.report_id)

        try:
            async with running_asgi(app) as base:
                settings.public_base_url = base
                app.state.service_access.settings.public_base_url = base
                app.state.grip = GripService(app, settings)
                async with LoomClient(base, token=settings.grip_token, allow_insecure_grip=True) as sdk:
                    package = await ServicePackage(ROOT / "capability_services/suanli_overleaf_http").publish(sdk, image_ref=IMAGE)
                    slave = (await sdk.resources(mcue="computing/slave"))[0]["uri"]
                    activated = await sdk.invoke(slave, sdk.names.action("activate-package"), {
                        "package": package, "instance_name": "overleaf", "instance_config": {}, "idempotency_key": "activate"})
                    assert activated["state"] == "pending_auth"
                    target = activated["resource"]
                    started = await sdk.invoke(target, sdk.names.action("auth-start"), {"expected_revision": activated["revision"]})
                    info = started["authentication"]["client"]
                    assert started["authentication"]["cookie_name"] == "overleaf.sid"
                    async with httpx.AsyncClient(base_url=base) as browser:
                        headers = {"Authorization": "Bearer " + info["session_token"]}
                        opened = await browser.post("/api/v1/auth/browser/bridge", headers=headers,
                            json={"action": "open", "session_id": info["session_id"], "query": {}})
                        assert opened.status_code == 200
                        for cookie, expected in (("anonymous", "pending"), ("verified-session", "accepted")):
                            response = await browser.post("/api/v1/auth/browser/bridge", headers=headers, json={
                                "action": "callback", "session_id": info["session_id"], "browser_binding": opened.json()["browser_binding"],
                                "query": {"overleaf.sid": [cookie]}})
                            assert response.json() == {"status": expected}, response.text
                    await health()
                    description = await sdk.describe(target, refresh=True)
                    assert description["resource"]["metadata"]["state"] == "ready"
                    assert "verified-session" not in json.dumps(description)
                    client = await OverleafClient.discover(sdk, target)
                    assert len((await client.list())["projects"]) == 2
                    directory, state = tmp_path / "paper", tmp_path / "paper.sync.json"
                    downloaded = await client.download(PROJECT, directory, state)
                    assert not (await client.status(state))["changed"]
                    (directory / "main.tex").write_text("edited through public GRIP")
                    uploaded = await client.upload(directory, state, message="Public integration", idempotency_key="edit")
                    assert uploaded["status"] == "uploaded" and uploaded["revision"] != downloaded["revision"]
                    assert not (await client.status(state))["changed"]
                    imported = await client.import_project(directory, name="New paper", idempotency_key="new")
                    assert imported["project_id"] == IMPORTED
                    assert await client.import_project(directory, name="New paper", idempotency_key="new") == imported
                    assert not repo.runs
                    result = await sdk.invoke(target, sdk.names.action("stop"), {
                        "expected_revision": activated["revision"], "idempotency_key": "stop"})
                    assert result["state"] == "stopped"
        finally:
            await app.state.driver.stop_execution_loop()
            await app.state.service_access.http.aclose()
            for slave in app.state.slaves.values():
                await slave.close()
