import sys
import subprocess
import zipfile
import json
from pathlib import Path

import httpx
import pytest

from loom_orchestration import ClosureContract, GripError, LoomClient, OrchestratorPackage, ResourceEffect, ResourceRef, SubmissionBuilder, TaskClosure
from loom_v2.observer.app import create_app
from loom_v2.settings import Settings
from loom_v2.observer.repository import ObserverRepository
from loom_v2.testing.observer import seed_embedded_slaves
from tests.api.test_public_resource_effect_metadata import _register_packages


@pytest.mark.asyncio
async def test_public_client_can_discover_metadata_and_resolve_contracts():
    repo = ObserverRepository()
    await _register_packages(repo)
    settings = Settings(grip_token="test-token", public_base_url="http://observer")
    async with LoomClient("http://observer", token="test-token", allow_insecure_grip=True, transport=httpx.ASGITransport(app=create_app(repo, settings=settings))) as client:
        packages = await client.packages()
        cloud = next(package for package in packages if package["package_id"] == "suanlicloud-http")
        export = cloud["action_exports"][0]
        descriptor = await client.descriptor(ResourceRef.model_validate(export["action_descriptor_ref"]))
        effect = ResourceEffect.model_validate(descriptor["resource_effects"][0])
        assert effect.readiness.result_path == "result.InstanceStatuses[]"
        contract = await client.read_json(ResourceRef.model_validate(export["io_contract_ref"]))
        schema = await client.read_json(ResourceRef.model_validate(contract["output_schema_ref"]))
        assert schema["properties"]["result"]["properties"]["Instances"]["items"]["required"] == ["InstanceId"]
        assert await client.resources(mcue="computing/slave") == []


@pytest.mark.asyncio
async def test_public_client_roundtrips_uploaded_descriptor():
    async with LoomClient("http://observer", token="test-token", allow_insecure_grip=True, transport=httpx.ASGITransport(app=create_app(settings=Settings(grip_token="test-token", public_base_url="http://observer")))) as client:
        descriptor = {"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/public", "name": "public"}
        ref = await client.upload_content(descriptor, media_type="application/vnd.loom.action-descriptor+json")
        assert ref.identity_criterion == "descriptor_digest"
        assert ref.version_or_digest
        assert await client.descriptor(ref) == descriptor
        assert await client.read_json(ref) == descriptor


@pytest.fixture
async def native_client():
    settings = Settings(grip_token="test-token", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    requests = []

    async def capture(request):
        requests.append(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(repo, settings=settings)),
        event_hooks={"request": [capture]},
        headers={"Authorization": "Bearer unused-header", "Cookie": "unused=cookie"},
    ) as transport:
        async with LoomClient("http://observer", token="test-token", client=transport,
                              allow_insecure_grip=True) as client:
            yield client, repo, requests


def native_plan():
    return ClosureContract(closure_id="sdk-native", goal="execute native task",
                           body=TaskClosure(closure_id="sdk-native"))


async def settle_native(repo, run_id, terminal_state):
    claim = await repo.claim_next_execution(
        workspace_id="workspace-default", driver_id="driver-test",
        driver_instance_id="driver-instance", driver_epoch=1,
    )
    assert claim["run"]["run_id"] == run_id
    record = await repo.get_run(run_id)
    return await repo.record_result(run_id, {
        "attempt_id": record.attempts[-1]["attempt_id"], "execution_id": record.execution_id,
        "execution_epoch": record.execution_epoch, "terminal_state": terminal_state,
        "value": {"answer": 42},
        "terminal_error": {"code": "execution_failed"} if terminal_state == "failed" else None,
    })


async def test_public_client_native_submission_read_cancel_and_close_use_grip(native_client):
    client, repo, requests = native_client
    key = "sdk/business-key with spaces"
    first = await client.submit(request_id=key, plan=native_plan(), conversation_ref="conversation-sdk")
    assert (await client.submit(request_id=key, plan=native_plan(), conversation_ref="conversation-sdk"))["run"] == first["run"]
    assert await client.submission(key) == first
    assert first["execution_request_state"] == "queued"
    assert first["conversation_ref"] == "conversation-sdk"
    assert len(repo.execution_requests) == 1
    by_uri = await client.run(first["run"])
    assert await client.run(first["run_id"]) == by_uri
    assert by_uri["state"] == "committed"
    assert by_uri["plan"] == first["plan"]
    assert by_uri["events"]
    frozen = await client.invoke(first["plan"], client.names.action("read"), {})
    assert frozen["closure_version"] == first["closure_version"]
    assert frozen["plan"] == first["closure_contract"]
    # No cancellation id is needed, including for a Run received as a URI.
    assert (await client.cancel(first["run"]))["state"] == "cancelled"
    assert (await client.cancel(first["run_id"], request_id=key, reason="stop requested"))["state"] == "cancelled"
    assert (await client.close(first["run"]))["state"] == "closed"
    assert (await client.close(first["run_id"]))["state"] == "closed"
    messages = [json.loads(request.content) for request in requests]
    assert all(request.url.path == "/.well-known/grip" and request.method == "POST" for request in requests)
    assert all("authorization" not in request.headers and "cookie" not in request.headers for request in requests)
    assert all(message["auth"] == {"scheme": "bearer", "credential": "test-token"} for message in messages)
    assert len({message["request_id"] for message in messages}) == len(messages)
    assert all(message["request_id"] != key for message in messages)
    calls = [message for message in messages if message["method"] == "INVOKE"]
    driver_discovery = [message for message in calls if message["action"] == client.names.action("list-resources")]
    assert len(driver_discovery) == 1
    assert driver_discovery[0]["input"] == {"mcue": "computing/driver"}
    cancellations = [message["input"] for message in calls if message["action"] == client.names.action("cancel")]
    assert cancellations == [{"reason": "user_cancel"}, {"reason": "stop requested", "idempotency_key": key}]
    schema_reads = {message["target"] for message in calls if message["action"] == client.names.action("read")}
    assert {client.names.schema(name) for name in (
        "submit-plan-input", "submit-plan-output", "get-submission-output",
        "read-run-input", "read-run-output", "read-plan-output",
    )} <= schema_reads


async def test_public_client_repairs_and_resolves_attestation_over_grip(native_client):
    client, repo, _ = native_client
    plan = native_plan()
    first = await client.submit(request_id="sdk-initial", plan=plan)
    original = await client.invoke(first["plan"], client.names.action("read"), {})
    pending = await settle_native(repo, first["run_id"], "failed")
    assert pending.outcome["decision"] == "repair"
    replacement = plan.model_copy(deep=True)
    replacement.body.metadata["repair"] = "second version"
    with pytest.raises(GripError, match="version_conflict"):
        await client.resubmit(first["run"], request_id="sdk-repair",
                              base_closure_version="stale", plan=replacement)
    repaired = await client.resubmit(first["run"], request_id="sdk-repair",
                                     base_closure_version=first["closure_version"], plan=replacement)
    assert repaired["run"] == first["run"]
    assert repaired["plan"] != first["plan"]
    assert repaired["closure_version"] != first["closure_version"]
    assert await client.resubmit(first["run_id"], request_id="sdk-repair",
                                 base_closure_version=first["closure_version"], plan=replacement) == repaired
    assert await client.invoke(first["plan"], client.names.action("read"), {}) == original
    pending = await settle_native(repo, first["run_id"], "decision_required")
    assert pending.outcome["decision"] == "attestation"
    accepted = await client.resolve(first["run"], "accept")
    assert accepted["state"] == "completed"
    assert accepted["outcome"]["value"] == {"answer": 42}
    assert await client.resolve(first["run_id"], "accept") == accepted
    assert (await client.close(first["run"]))["state"] == "closed"


async def test_public_client_can_abandon_repair_over_grip(native_client):
    client, repo, _ = native_client
    first = await client.submit(request_id="sdk-abandon", plan=native_plan())
    await settle_native(repo, first["run_id"], "failed")
    assert (await client.resolve(first["run"], "abandon"))["state"] == "failed"


async def test_native_client_validates_action_inputs_before_sending(native_client):
    client, _, requests = native_client
    with pytest.raises(GripError, match="invalid_input"):
        await client.submit(request_id="", plan=native_plan())
    assert not any(json.loads(request.content).get("action") == client.names.action("submit-plan") for request in requests)
    first = await client.submit(request_id="sdk-validation", plan=native_plan())
    requests.clear()
    with pytest.raises(GripError, match="invalid_input"):
        await client.invoke(first["run"], client.names.action("read"), {"include_history": "true"})
    with pytest.raises(GripError, match="invalid_input"):
        await client.cancel(first["run"], request_id="")
    assert not any(json.loads(request.content).get("method") == "INVOKE"
                   and json.loads(request.content)["target"] == first["run"] for request in requests)


@pytest.mark.parametrize("token,error", [(None, "authentication_required"), ("wrong-token", "authentication_failed")])
async def test_native_client_requires_grip_message_auth(token, error):
    settings = Settings(grip_token="test-token", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    async with LoomClient("http://observer", token=token, allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=create_app(repo, settings=settings))) as client:
        with pytest.raises(GripError, match=error):
            await client.submit(request_id="unauthorized", plan=native_plan())
    assert not repo.execution_requests


@pytest.mark.asyncio
async def test_public_sdk_uploads_source_and_submits_complete_closure():
    repo = ObserverRepository()
    seed_embedded_slaves(repo)
    repo.orchestrator_runtime_available = False
    async with LoomClient("http://observer", token="test-token", allow_insecure_grip=True, transport=httpx.ASGITransport(app=create_app(repo, settings=Settings(grip_token="test-token", public_base_url="http://observer")))) as client:
        contract = await client.upload_content(
            {"schema_version": "io.v1"}, media_type="application/vnd.loom.io-contract+json",
        )
        node_descriptor = await client.upload_content(
            {"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/echo", "name": "echo"},
            media_type="application/vnd.loom.action-descriptor+json",
        )
        node_source = await client.upload_content(
            "import json,sys; print(json.dumps(json.load(sys.stdin)))", media_type="text/x-python",
        )
        node = await client.register_package({
            "package_id": "sdk-echo", "package_version": "v1", "package_type": "function",
            "execution": {"kind": "process:json_stdio", "version": "1"},
            "action_exports": [{
                "action_descriptor_ref": node_descriptor.model_dump(mode="json"),
                "io_contract_ref": contract.model_dump(mode="json"),
                "effect_class": "Sandboxed", "permissions": [],
                "replay_safety": "DeterministicByEventLog", "runtime_binding": {},
            }],
            "body": {"program_content_ref": node_source.model_dump(mode="json")},
        })
        node_ref = ResourceRef(
            resource_id=node["resource"],
            version_or_digest=node["package_digest"],
        )
        source = (
            'async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":\n'
            f'    handle = ctx.emit_node("echo", {node_ref.model_dump(mode="json")!r}, '
            f'{node_descriptor.model_dump(mode="json")!r}, [input_ref])\n'
            '    return await ctx.result(handle)\n'
        )
        program = await client.upload_content(source, media_type="text/x-python")
        descriptor = await client.upload_content(
            {"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/orchestrate", "name": "orchestrate"},
            media_type="application/vnd.loom.action-descriptor+json",
        )
        package = await client.register_package(OrchestratorPackage(
            program_content_ref=program, allowed_node_package_refs=[node_ref],
            action_descriptor_ref=descriptor, io_contract_ref=contract,
        ).manifest())
        input_ref = await client.upload_content({"hello": "world"})
        plan = SubmissionBuilder(
            goal="echo", body=TaskClosure(program={
                "action_ref": "grip://loom.local/workspaces/workspace-default/actions/orchestrate",
                "io_contract_ref": contract.model_dump(mode="json"),
            }),
            orchestrator_package=ResourceRef(
                resource_id=package["resource"],
                version_or_digest=package["package_digest"],
            ),
            input_binding=input_ref,
        ).build(request_id="sdk-submit")
        first = await client.submit(request_id="sdk-submit", plan=plan)
        second = await client.submit(request_id="sdk-submit", plan=plan)
        assert first["run_id"] == second["run_id"]
        assert first["execution_request_state"] == "queued"
        assert package["body"]["program_content_ref"]["resource_id"] == program.resource_id
        assert await client.read_json(input_ref) == {"hello": "world"}


def test_skill_bundle_contains_installable_standalone_sdk(tmp_path):
    root = Path(__file__).resolve().parents[2]
    subprocess.run([
        sys.executable, str(root / "scripts/build_agent_skill.py"), "--output", str(tmp_path),
    ], check=True, capture_output=True, text=True)
    with zipfile.ZipFile(tmp_path / "personal-computing-space.zip") as archive:
        archive.extractall(tmp_path / "unpacked")
    skill = tmp_path / "unpacked" / "personal-computing-space"
    wheel, = (skill / "assets" / "wheels").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.startswith("loom_v2/") for name in archive.namelist())
    target = tmp_path / "installed"
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel),
    ], check=True, capture_output=True, text=True)
    subprocess.run([
        sys.executable, "-I", "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import loom_orchestration; "
        "assert loom_orchestration.__file__.startswith(sys.argv[1]); "
        "from loom_orchestration import ResourceEffect, LoomClient, ProgramSource, ServicePackage, GripError; "
        "assert not any(name == 'loom_v2' or name.startswith('loom_v2.') for name in sys.modules)",
        str(target),
    ], cwd=tmp_path, check=True, capture_output=True, text=True)
