import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.types import ClosureContract, TaskClosure
from loom_v2.contracts.errors import DomainError
from loom_v2.testing.observer import create_embedded_app as create_app
from loom_v2.observer.repository import ObserverRepository


def _binding(operation: str = "run_code", target: str = "slave-a", *, package_ref: dict | None = None, descriptor_ref: dict | None = None) -> dict:
    return {
        "binding_id": f"binding-{operation}",
        "hole_id": "h_run_code",
        "capability_descriptor_ref": descriptor_ref or {"resource_id": "executor://process:json_stdio/1"},
        "capability_package_ref": package_ref,
        "target_resource_ref": {"resource_id": target},
        "bound_by": "test-driver",
    }


def _add_run_code_hole(client: TestClient, suffix: str = "default"):
    plan = {
        "closure_id": f"closure-readiness-{suffix}",
        "goal": "run test code",
        "body": {
            "closure_id": f"closure-readiness-{suffix}",
            "program": {"operation_ref": "loom://run_code"},
            "compute": {"operation_ref": "loom://run_code", "typed_holes": [{"hole_id": "h_run_code"}]},
        },
    }
    response = client.post(
        "/api/v1/run-submissions",
        json={"request_id": f"run-code-{suffix}", "conversation_ref": "task-readiness", "plan": plan},
    )
    return plan, response


def test_unbound_typed_hole_blocks_submission_and_reports_readiness():
    client = TestClient(create_app())
    _, response = _add_run_code_hole(client, "unbound")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "readiness_blocked"
    assert response.json()["detail"]["details"]["blockers"] == [{"code": "typed_hole_unbound", "hole_id": "h_run_code"}]


def test_run_code_binding_requires_package_and_then_allows_submission():
    client = TestClient(create_app())
    plan, _ = _add_run_code_hole(client, "package")
    body = plan["body"]
    body["compute"]["typed_holes"][0].update(status="bound", binding_ref="binding-run_code")
    body["compute_bindings"] = [_binding()]
    request = {"request_id": "package-required", "plan": plan}
    blocked = client.post("/api/v1/run-submissions", json=request)
    assert blocked.status_code == 409
    assert any(b["code"] == "capability_package_required" for b in blocked.json()["detail"]["details"]["blockers"])

    def put(content, media_type):
        response = client.post("/api/v1/content", json={"content": content, "media_type": media_type})
        assert response.status_code == 200, response.text
        return response.json()

    program = put("import json,sys; print(json.dumps(json.load(sys.stdin)))", "text/x-python")
    io = put({"schema_version": "io.v1"}, "application/vnd.loom.io-contract+json")
    descriptor = put({"schema_version": "operation.v1", "resource_id": "loom://run_code", "name": "run_code"}, "application/vnd.loom.operation-descriptor+json")
    package = client.post("/api/v1/capability-packages", json={
        "package_id": "readiness-run-code", "package_version": "v1",
        "capability_exports": [{"capability_descriptor_ref": descriptor, "io_contract_ref": io,
                                "effect_class": "Sandboxed", "replay_safety": "DeclaredByPackage"}],
        "body": {"program_content_ref": program},
    })
    assert package.status_code == 200, package.text
    body["program"]["io_contract_ref"] = io
    body["compute_bindings"] = [_binding(
        descriptor_ref=descriptor,
        package_ref={"resource_id": "capability-package://readiness-run-code/v1",
                     "version_or_digest": package.json()["package_digest"]},
    )]
    submitted = client.post("/api/v1/run-submissions", json=request)
    assert submitted.status_code == 202, submitted.text
    assert client.get(f"/api/v1/runs/{submitted.json()['run_id']}/readiness").json()["ready"] is True


@pytest.mark.asyncio
async def test_input_schema_requires_bound_ref_and_rejects_wrapped_payload() -> None:
    repo = ObserverRepository()
    schema_ref = await repo.put_content(
        {"type": "object", "required": ["scores"], "properties": {"scores": {"type": "array"}}},
        media_type="application/schema+json",
    )
    contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": schema_ref.model_dump(mode="json"),
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    contract = ClosureContract(
        closure_id="closure-input-schema",
        goal="validate scores",
        body=TaskClosure(
            closure_id="closure-input-schema",
            program={"operation_ref": "loom://test_input_validation", "io_contract_ref": contract_ref.model_dump(mode="json")},
        ),
    )
    record = await repo.open_run("run-input-schema", "task-input-schema", "validate scores", closure_contract=contract)

    missing = await repo.inspect_readiness(record.run_id)
    assert missing["ready"] is False
    assert any(item["code"] == "payload_missing" for item in missing["blockers"])

    wrapped_ref = await repo.put_content({"payload": {"scores": [80]}}, media_type="application/json")
    wrapped = await repo.apply_patch(
        record.run_id,
        record.draft_version,
        record.draft_digest,
        "input-wrapped",
        [{"kind": "set_execution_payload", "value": {"node_id": "loom://test_input_validation", "input_ref": wrapped_ref.model_dump(mode="json")}}],
    )
    mismatch = next(item for item in wrapped.readiness["blockers"] if item["code"] == "payload_schema_mismatch")
    assert mismatch["schema_digest"] == schema_ref.digest
    assert mismatch["errors"][0]["keyword"] == "required"

    input_ref = await repo.put_content({"scores": [80]}, media_type="application/json")
    valid = await repo.apply_patch(
        record.run_id,
        wrapped.draft_version,
        wrapped.draft_digest,
        "input-valid",
        [{"kind": "set_execution_payload", "value": {"node_id": "loom://test_input_validation", "input_ref": input_ref.model_dump(mode="json")}}],
    )
    assert not any(item["code"].startswith("payload_") for item in valid.readiness["blockers"])


@pytest.mark.asyncio
async def test_commit_and_start_reuse_input_readiness_blockers() -> None:
    repo = ObserverRepository()
    schema_ref = await repo.put_content({"type": "object", "required": ["value"]}, media_type="application/schema+json")
    contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": schema_ref.model_dump(mode="json"),
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    contract = ClosureContract(
        closure_id="closure-commit-input",
        goal="validate input",
        body=TaskClosure(
            closure_id="closure-commit-input",
            program={"operation_ref": "loom://test_input_validation", "io_contract_ref": contract_ref.model_dump(mode="json")},
        ),
    )
    record = await repo.open_run("run-commit-input", "task-commit-input", "validate input", closure_contract=contract)
    await repo.begin_refinement(record.run_id)

    with pytest.raises(DomainError) as caught:
        await repo.commit(record.run_id, record.draft_version, record.draft_digest)
    assert caught.value.envelope.code == "readiness_blocked"
    assert caught.value.envelope.details["blockers"][0]["code"] == "payload_missing"


@pytest.mark.asyncio
async def test_program_inline_io_fields_are_not_an_executable_contract() -> None:
    repo = ObserverRepository()
    contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": None,
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    contract = ClosureContract(
        closure_id="closure-inline-schema",
        goal="validate input",
        body=TaskClosure(
            closure_id="closure-inline-schema",
            program={
                "operation_ref": "loom://test_input_validation",
                "io_contract_ref": contract_ref.model_dump(mode="json"),
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "success_semantics": {"criterion": "validated"},
            },
        ),
    )
    record = await repo.open_run("run-inline-schema", "task-inline-schema", "validate input", closure_contract=contract)

    readiness = await repo.inspect_readiness(record.run_id)

    assert readiness["ready"] is False
    blocker = next(item for item in readiness["blockers"] if item["code"] == "inline_io_contract_forbidden")
    assert blocker == {
        "code": "inline_io_contract_forbidden",
        "fields": ["input_schema", "output_schema", "success_semantics"],
    }
