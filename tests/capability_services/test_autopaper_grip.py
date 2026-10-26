"""Publish, activate, authenticate and invoke AutoPaper through the real GRIP stack."""
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import httpx
import pytest

from capability_services.autopaper_http.server import AutoPaperGateway, Handler
from loom_orchestration import GripError, LoomClient, ServicePackage
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.runtime_plugins import RuntimePluginHost
from loom_v2.testing.observer import create_embedded_app, seed_embedded_slaves
from runtime_plugins.container_http_v1.runtime import DockerContainerHTTPRuntimeV1
from tests.capability_services.test_autopaper_gateway import PAPER, provider
from tests.capability_services.test_grip_services import (
    DockerFixture, IMAGE, LocalGatewayTransport, ROOT, RuntimeAdapter, serve, wait_result,
)


async def test_autopaper_login_every_export_and_reauthentication(tmp_path):
    authorized = {"allow": True, "failure": None}
    calls = []
    def upstream(request):
        calls.append(request)
        if not authorized["allow"]:
            return httpx.Response(401)
        if authorized["failure"] == "invalid":
            return httpx.Response(200, json={"private": "wrong shape"})
        if authorized["failure"] == "unavailable":
            return httpx.Response(503, text="private upstream detail")
        return provider(request)

    gateway = AutoPaperGateway(instance_path=tmp_path / "instance.json", state_root=tmp_path / "state",
                              transport=httpx.MockTransport(upstream))
    handler = type("AutoPaperHandler", (Handler,), {"gateway": gateway})
    key = Ed25519PrivateKey.generate()
    (tmp_path / "sign.pem").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    (tmp_path / "verify.pem").write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(grip_token="autopaper-test", public_base_url="http://observer",
                        coding_agent_backend="none", agent_lease_ttl_seconds=300,
                        grip_signing_key_file=str(tmp_path / "sign.pem"))
    settings.grip_verification_keys = {settings.grip_signing_key_id: str(tmp_path / "verify.pem")}
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services/autopaper_http")
    runner = DockerFixture()
    async with serve(handler) as port:
        transport = LocalGatewayTransport(port)
        runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", egress_network="egress",
            runner=runner, http_transport=transport, instance_state_dir=tmp_path / "instances")
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
                slave = (await client.resources(mcue="computing/slave"))[0]["uri"]
                activated = await client.invoke(slave, client.names.action("activate-package"), {
                    "package": package, "instance_name": "paper", "instance_config": {}, "idempotency_key": "activate"})
                assert activated["state"] == "pending_auth"
                target = activated["resource"]
                pending = (await client.describe(target))["resource"]["metadata"]
                assert len(pending["actions"]) == 6 and all(not a["available"] for a in pending["actions"])

                for attempt in range(2):
                    started = await client.invoke(target, client.names.action("auth-start"), {
                        "expected_revision": activated["revision"]})
                    info = started["authentication"]["client"]
                    assert started["authentication"]["method"] == "browser-cookie"
                    assert started["authentication"]["cookie_name"] == "ap_sid"
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://observer") as browser:
                        headers = {"Authorization": "Bearer " + info["session_token"]}
                        opened = await browser.post("/api/v1/auth/browser/bridge", headers=headers,
                            json={"action": "open", "session_id": info["session_id"], "query": {}})
                        assert opened.status_code == 200, opened.text
                        assert opened.json()["location"] == "https://paper.cs.ac.cn/oauth2/login"
                        callback = {"action": "callback", "session_id": info["session_id"],
                                    "browser_binding": opened.json()["browser_binding"],
                                    "query": {"ap_sid": ["private-session"]}}
                        accepted = await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)
                        assert accepted.status_code == 200 and accepted.json() == {"status": "accepted"}
                        assert (await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)).status_code == 404
                    await report_health()
                    description = await client.describe(target, refresh=True)
                    ready = description["resource"]["metadata"]
                    assert ready["state"] == "ready" and ready["revision"] == activated["revision"]
                    assert all(a["available"] for a in ready["actions"])
                    assert "private-session" not in json.dumps(description)
                    assert info["session_token"] not in json.dumps(description)
                    actions = {a["name"]: a["uri"] for a in ready["actions"]}
                    assert (await client.invoke(target, actions["papers_get"], {"paper_id": "paper-1"}))["result"] == PAPER
                    if attempt == 0:
                        authorized["allow"] = False
                        await report_health()
                        expired = (await client.describe(target, refresh=True))["resource"]["metadata"]
                        assert expired["state"] == "pending_auth"
                        assert expired["authentication"]["status"] == "auth_required"
                        authorized["allow"] = True

                arguments = {"papers_list": {"venue": "ACL", "year": 2025}, "venues_list": {},
                             "years_list": {"venue": "ACL"}, "venues_catalog": {}}
                for name, payload in arguments.items():
                    result = await client.invoke(target, actions[name], payload)
                    assert result["action"] == name and result["result"]
                for failure in ("invalid", "unavailable"):
                    authorized["failure"] = failure
                    with pytest.raises(GripError):
                        await client.invoke(target, actions["papers_get"], {"paper_id": "paper-1"})
                authorized["failure"] = None
                with pytest.raises(GripError, match="invalid_input"):
                    await client.invoke(target, actions["papers_get"], {"paper_id": "../admin"})

                payload = {"arguments": {"keywords": ["RAG"], "top_k": 5},
                           "expected_revision": activated["revision"], "idempotency_key": "search-once"}
                submitted = await client.invoke(target, actions["papers_search"], payload)
                assert await client.invoke(target, actions["papers_search"], payload) == submitted
                outcome = await wait_result(app, client, submitted["run"])
                assert outcome["state"] == "completed", outcome
                assert outcome["outcome"]["value"]["result"]["papers"][0]["title"] == PAPER["title"]
                searches = [r for r in calls if r.url.path == "/api/search"]
                assert len(searches) == 1
                assert json.loads(searches[0].content)["auto_ingest"] is False
                assert "private-session" not in json.dumps(outcome)
                assert "private-session" not in json.dumps(await client.resources())
                created = [c for c in runner.calls if c[0] == "create"]
                assert len(created) == 1
                assert any("target=/var/lib/loom/service" in str(arg) for arg in created[0])
                stopped = await client.invoke(target, client.names.action("stop"), {
                    "expected_revision": activated["revision"], "idempotency_key": "stop"})
                assert stopped["state"] == "stopped"
        finally:
            await app.state.driver.stop_execution_loop()
            for slave in app.state.slaves.values():
                await slave.close()
            gateway.close()
