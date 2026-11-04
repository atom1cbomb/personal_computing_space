"""Publish the real CS101 artifacts and exercise direct and task GRIP calls."""
import json

import httpx
import pytest

from capability_services.cs101_http.server import CS101Gateway, Handler
from loom_orchestration import GripError, LoomClient, ServicePackage
from loom_v2.pool.repository import PoolRepository as ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.runtime_plugins import RuntimePluginHost
from loom_v2.testing.observer import create_embedded_app, seed_embedded_slaves
from runtime_plugins.container_http_v1.runtime import DockerContainerHTTPRuntimeV1
from tests.capability_services.test_cs101_gateway import provider
from tests.capability_services.test_grip_services import (
    DockerFixture, IMAGE, LocalGatewayTransport, ROOT, RuntimeAdapter,
    configure_grip_keys, serve, wait_result,
)
from tests.support.action_plan import submit_activation_plan


@pytest.mark.parametrize("use_profile", [True, False])
async def test_cs101_publish_authenticate_every_export_and_reauthenticate(tmp_path, use_profile):
    config = {"api_origin": "https://test.cs101.gbu.edu.cn"}
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps(config))
    profiles = tmp_path / "profiles"
    credentials = profiles / "cs101-test"
    credentials.mkdir(parents=True)
    token = credentials / "cs101_token"
    token.write_text("private-cs101-fixture")
    control = {"status": 200, "token": "private-cs101-fixture"}
    if not use_profile:
        token.unlink()
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.url.host == "test.cs101.gbu.edu.cn"
        assert request.method == "GET"
        if request.headers["Authorization"] != "Bearer " + control["token"]:
            return httpx.Response(401)
        return provider(request) if control["status"] == 200 else httpx.Response(control["status"])

    gateway = CS101Gateway(instance_path=instance, credential_root=credentials, state_root=tmp_path / "state",
                           transport=httpx.MockTransport(upstream))
    handler = type("CS101Handler", (Handler,), {"gateway": gateway})
    settings = Settings(grip_token="cs101-test", public_base_url="http://observer",
                        coding_agent_backend="none", agent_lease_ttl_seconds=300)
    configure_grip_keys(settings, tmp_path)
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    bundle = ServicePackage(ROOT / "capability_services/cs101_http")
    async with serve(handler) as port:
        runner = DockerFixture()
        runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", egress_network="egress",
            runner=runner, http_transport=LocalGatewayTransport(port), credential_dir=profiles,
            instance_state_dir=tmp_path / "instances")
        worker = app.state.slaves["slave-a"]
        worker.runtime_plugin_host = RuntimePluginHost(plugins={('service', 'container:http', '1'): RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo, app.state.slaves)

        async def report_health():
            for report in await worker.inspect_activation_health():
                await repo.record_capability_health(report)
                worker.acknowledge_health_report(report.report_id)

        async def login_browser(client, target, revision):
            started = await client.invoke(target, client.names.action("auth-start"), {"expected_revision": revision})
            auth = started["authentication"]
            assert auth["method"] == "browser-storage" and auth["storage_key"] == "token"
            info = auth["client"]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://observer") as browser:
                headers = {"Authorization": "Bearer " + info["session_token"]}
                opened = await browser.post("/api/v1/auth/browser/bridge", headers=headers,
                    json={"action": "open", "session_id": info["session_id"]})
                assert opened.status_code == 200
                assert opened.json()["location"] == config["api_origin"] + "/login?manual=show"
                callback = {"action": "callback", "session_id": info["session_id"],
                            "browser_binding": opened.json()["browser_binding"],
                            "query": {"token": [json.dumps(control["token"])]}}
                control["status"] = 401
                pending = await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)
                assert pending.status_code == 200 and pending.json() == {"status": "pending"}
                control["status"] = 200
                accepted = await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)
                assert accepted.status_code == 200 and accepted.json() == {"status": "accepted"}
                assert (await browser.post("/api/v1/auth/browser/bridge", headers=headers, json=callback)).status_code == 404
            await report_health()

        try:
            async with LoomClient("http://observer", token=settings.grip_token,
                    transport=httpx.ASGITransport(app=app), allow_insecure_grip=True) as client:
                package = await bundle.publish(client, image_ref=IMAGE)
                slave = (await client.resources(mcue="computing/slave"))[0]["uri"]
                check = await client.invoke(slave, client.names.action("check-package"), {
                    "package": package, "instance_name": "grades", "instance_config": {}})
                assert check["ready"] is False
                activated = await client.invoke(slave, client.names.action("activate-package"), {
                    "package": package, "instance_name": "grades", "instance_config": config,
                    "credential_profile": "cs101-test" if use_profile else None, "idempotency_key": "activate-grades"})
                assert activated["state"] == ("ready" if use_profile else "pending_auth")
                target = activated["resource"]
                if not use_profile:
                    await login_browser(client, target, activated["revision"])
                description = await client.describe(target)
                assert description["resource"]["metadata"]["authentication"]["status"] == "authenticated"
                usage = await client.invoke(package, client.names.action("how-to-use"), {})
                assert "iaaa.gbu.edu.cn" in json.dumps(usage)
                actions = {a["name"]: a["uri"] for a in description["resource"]["metadata"]["actions"]}
                arguments = {"papers_list": {}, "paper_statistics": {"paper_id": 1},
                             "grades_list": {"paper_id": 1}, "grades_analyze": {"paper_id": 1},
                             "student_analyze": {"user_id": 10}}
                for name, payload in arguments.items():
                    result = await client.invoke(target, actions[name], payload)
                    assert result["action"] == name
                direct = await client.invoke(target, actions["grades_analyze"], {
                    "paper_id": 1, "max_score": 100, "pass_score": 60})
                assert direct["result"]["summary"]["mean"] == 60
                submitted = await submit_activation_plan(client, target, actions["grades_analyze"],
                    {"paper_id": 1, "max_score": 100, "pass_score": 60}, "grades-once")
                assert await client.submission("grades-once") == submitted
                outcome = await wait_result(app, client, submitted["run"])
                assert outcome["state"] == "completed", outcome
                assert outcome["outcome"]["value"]["result"]["summary"] == direct["result"]["summary"]
                for invalid in ({"paper_id": True}, {"paper_id": 1, "url": "https://iaaa.gbu.edu.cn"}):
                    with pytest.raises(GripError, match="invalid_input"):
                        await client.invoke(target, actions["grades_analyze"], invalid)

                control["status"] = 401
                await report_health()
                expired = (await client.describe(target, refresh=True))["resource"]["metadata"]
                assert expired["state"] == "pending_auth"
                assert expired["authentication"]["status"] == "auth_required"
                control["token"] = "renewed-private-cs101-fixture"
                if use_profile:
                    token.write_text(control["token"])
                control["status"] = 200
                if not use_profile:
                    await login_browser(client, target, expired["revision"])
                await report_health()
                ready = await client.describe(target, refresh=True)
                assert ready["resource"]["metadata"]["state"] == "ready"
                assert "private-cs101-fixture" not in json.dumps([ready, outcome, await client.resources()])
                creates = [args for args in runner.calls if args[0] == "create"]
                assert len(creates) == 1
                assert any(f"src={credentials},dst=/run/loom/credentials,readonly" in arg for arg in creates[0]) == use_profile
                assert any("target=/var/lib/loom/service" in arg for arg in creates[0])
                stopped = await client.invoke(target, client.names.action("stop"), {
                    "expected_revision": ready["resource"]["metadata"]["revision"], "idempotency_key": "stop-grades"})
                assert stopped["state"] == "stopped"
        finally:
            await app.state.driver.stop_execution_loop()
            for slave in app.state.slaves.values():
                await slave.close()
            gateway.close()
