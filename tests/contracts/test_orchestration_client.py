import sys
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from loom_orchestration import LoomClient, OrchestratorPackage, ResourceEffect, ResourceRef, SubmissionBuilder, TaskClosure
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


async def test_public_client_cancel_does_not_require_a_new_request_id():
    captured = []

    def respond(request):
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"state": "cancelled"})

    async with LoomClient("http://observer", transport=httpx.MockTransport(respond)) as client:
        assert (await client.cancel("run-test"))["state"] == "cancelled"
    assert captured == [{"request_id": None, "reason": "user_cancel"}]


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
