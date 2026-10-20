import asyncio

import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.agents import AgentRegistration, DriverCommand
from loom_v2.contracts.types import CapabilityHealthReport, ClosureContract, ComputeBinding, ResourceRef, TaskClosure
from loom_v2.observer.repository import ObserverRepository
from loom_v2.testing.observer import create_embedded_app
from loom_v2.testing.observer import seed_embedded_slaves
from tests.support.grip_client import invoke, resources
from tests.support.package_fixture import process_package_value, register_test_descriptor, action_export


async def _empty_io_contract_ref(repo: ObserverRepository):
    return await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": None,
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )


async def _program_ref(repo: ObserverRepository):
    return await repo.put_content("print(1)", media_type="text/x-python")


@pytest.mark.asyncio
async def test_user_upload_registers_workspace_package_without_run() -> None:
    repo = ObserverRepository()
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    manifest = process_package_value(
        repo,
        package_id="uploaded-package",
        action_ref="grip://loom.local/workspaces/workspace-default/actions/uploaded",
        program_content_ref=program_ref,
        io_contract_ref=io_contract_ref,
    )

    with TestClient(create_embedded_app(repo)) as client:
        response = client.post("/api/v1/capability-packages", json=manifest)
        assert response.status_code == 200, response.text
        package = response.json()
        assert package["scope"] == "workspace_reusable"
        assert package["publication_state"] == "published"
        assert package["source_run_ref"] is None
        assert package["workspace_id"] == "workspace-default"
        assert package["resource"] == "grip://loom.local/workspaces/workspace-default/packages/uploaded-package/v1"
        publication_url = f"/api/v1/capability-packages/{package['resource']}/promote"
        assert client.post(publication_url, json={"approved_digest": package["package_digest"], "target_slaves": ["slave-a"]}).status_code == 422
        promoted = client.post(publication_url, json={"approved_digest": package["package_digest"], "idempotency_key": "publish-only"})
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["resource"] == package["resource"]
        assert resources(client, "activation") == []

        listed = client.get("/api/v1/capability-packages").json()
        assert [item["package_id"] for item in listed].count("uploaded-package") == 1

    run = await repo.open_run("run-after-upload", "conversation", "use package")
    visible = await repo.list_capability_packages(run_id=run.run_id)
    assert any(item.package_id == "uploaded-package" for item in visible)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_bound", [False, True])
async def test_uploaded_workspace_package_can_be_activated_without_run(resource_bound) -> None:
    repo = ObserverRepository()
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    manifest = process_package_value(
        repo,
        package_id="uploaded-activation",
        action_ref="grip://loom.local/workspaces/workspace-default/actions/uploaded-activation",
        program_content_ref=program_ref,
        io_contract_ref=io_contract_ref,
    )
    with TestClient(create_embedded_app(repo)) as client:
        uploaded = client.post("/api/v1/capability-packages", json=manifest).json()
        n = client.app.state.grip.names
        package = uploaded["resource"]
        target_fields = {}
        if resource_bound:
            resource = invoke(client, n.root, n.action("register-resource"), {
                "resource_id": "grip://loom.local/workspaces/workspace-default/resources/test/vm", "package": package, "state": "running",
            })
            target_fields = {"target": resource["resource"], "instance_name": "vm-test"}
        response = invoke(client, n.name("slaves", "slave-a"), n.action("activate-package"), {
            "package": package, "idempotency_key": "activate-uploaded", **target_fields,
        })
        assert response["state"] == "ready"
        assert any(r["metadata"]["package"] == package for r in resources(client, "activation"))


@pytest.mark.asyncio
async def test_failed_activation_can_retry_without_cleanup():
    repo = ObserverRepository()
    io_ref = await _empty_io_contract_ref(repo)
    descriptor = register_test_descriptor(repo, "grip://loom.local/workspaces/workspace-default/actions/retryable-service/exec")
    package = await repo.register_workspace_package("workspace-default", {
        "package_type": "service",
        "package_id": "retryable-service",
        "package_version": "v1",
        "execution": {"kind": "container:http", "version": "1"},
        "action_exports": [action_export(descriptor, io_ref, effect_class="RemoteExecution", replay_safety="NotReplaySafe", runtime_binding={"path": "/v1/exec"})],
        "body": {"image_ref": "registry.example/service@sha256:" + "a" * 64, "container_port": 8080, "health_path": "/healthz"},
    })
    first = await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running", idempotency_key="attempt-1", instance_name="retry")
    assert first.activation_revision == 1
    await repo.record_capability_health(CapabilityHealthReport(
        report_id="failed-1", package_version_ref=package.version_ref, package_digest=package.package_digest,
        target_slave="slave-a", activation_state="failed", activation_revision=1, instance_name="retry",
    ))

    second = await repo.set_capability_desired(package.version_ref, "slave-a", desired_state="running", idempotency_key="attempt-2", instance_name="retry")
    assert second.activation_revision == 2
    assert second.activation_state == "provisioning"
    assert any(event["phase"] == "capability_activation_superseded" for event in repo.workspace_package_events[("workspace-default", package.version_ref)])
    with pytest.raises(ValueError, match="stale_activation_revision"):
        await repo.record_capability_health(CapabilityHealthReport(
            report_id="late-old", package_version_ref=package.version_ref, package_digest=package.package_digest,
            target_slave="slave-a", activation_state="ready", activation_revision=1, instance_name="retry",
        ))
    ready = await repo.record_capability_health(CapabilityHealthReport(
        report_id="ready-2", package_version_ref=package.version_ref, package_digest=package.package_digest,
        target_slave="slave-a", activation_state="ready", activation_revision=2, instance_name="retry",
    ))
    assert ready.activation_state == "ready"


@pytest.mark.asyncio
async def test_capability_get_command_normalizes_resource_ref_dict():
    repo = ObserverRepository()
    lease = await repo.register_agent(
        AgentRegistration(
            role="driver",
            agent_id="driver-default",
            instance_id="instance-1",
            workspace_id="workspace-default",
            endpoint_url="http://driver:8090",
            protocol_version="loom.v1",
        )
    )
    run = await repo.open_run("run-cg", "conversation-cg", "check")
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-cg",
        [
            {"kind": "set_program_ref", "value": "grip://loom.local/workspaces/workspace-default/actions/check"},
            {"kind": "add_typed_hole", "value": {"hole_id": "h"}},
            {"kind": "materialize_capability_package_candidate", "value": process_package_value(repo, package_id="cgpkg", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref)},
        ],
    )
    package = (await repo.get_run(run.run_id)).capability_packages[0]
    dict_ref = ResourceRef(
        resource_id=package.version_ref,
        version_or_digest=package.package_digest,
    ).model_dump(mode="json")
    result = await repo.execute_driver_command(
        DriverCommand(
            request_id="capability-get-1",
            driver_id="driver-default",
            instance_id="instance-1",
            lease_id=lease.lease_id,
            driver_epoch=lease.epoch,
            command="capability.get",
            arguments={"package_ref": dict_ref, "run_id": run.run_id, "workspace_id": "workspace-default"},
        )
    )
    assert result["package_id"] == "cgpkg"


@pytest.mark.asyncio
async def test_candidate_is_run_bound_and_cannot_be_used_by_another_run():
    repo = ObserverRepository()
    seed_embedded_slaves(repo)
    first = await repo.open_run("run-a", "conversation-a", "matmul")
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    patched = await repo.apply_patch(
        first.run_id,
        first.draft_version,
        first.draft_digest,
        "materialize-a",
        [
            {"kind": "set_program_ref", "value": "grip://loom.local/workspaces/workspace-default/actions/matmul"},
            {"kind": "add_typed_hole", "value": {"hole_id": "h"}},
            {"kind": "materialize_capability_package_candidate", "value": process_package_value(repo, package_id="pkg", action_ref="grip://loom.local/workspaces/workspace-default/actions/matmul", program_content_ref=program_ref, io_contract_ref=io_contract_ref)},
        ],
    )
    package = (await repo.get_run(first.run_id)).capability_packages[0]
    second = await repo.open_run("run-b", "conversation-b", "matmul")
    result = await repo.apply_patch(
        second.run_id,
        second.draft_version,
        second.draft_digest,
        "bind-b",
        [
            {"kind": "set_program_ref", "value": "grip://loom.local/workspaces/workspace-default/actions/matmul"},
            {"kind": "add_typed_hole", "value": {"hole_id": "h"}},
            {"kind": "bind_compute_hole", "value": ComputeBinding(
                binding_id="binding-b", hole_id="h", action_descriptor_ref=ResourceRef(resource_id="grip://loom.local/workspaces/workspace-default/resources/run-code"),
                capability_package_ref=ResourceRef(resource_id=package.version_ref), target_resource_ref=ResourceRef(resource_id="grip://loom.local/workspaces/workspace-default/slaves/slave-a"),

            ).model_dump(mode="json")},
        ],
    )
    assert any(blocker["code"] == "capability_package_scope_mismatch" for blocker in result.readiness["blockers"])
    assert all(item.package_id != "pkg" for item in await repo.list_capability_packages(run_id=second.run_id))


@pytest.mark.asyncio
async def test_same_run_package_coordinate_rejects_digest_conflict():
    repo = ObserverRepository()
    run = await repo.open_run("run-package-conflict", "conversation-package-conflict", "check")
    first_program_ref = await _program_ref(repo)
    second_program_ref = await repo.put_content("print(2)", media_type="text/x-python")
    io_contract_ref = await _empty_io_contract_ref(repo)
    first = await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-package-conflict-first",
        [
            {
                "kind": "materialize_capability_package_candidate",
                "value": process_package_value(repo, package_id="same-coordinate", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=first_program_ref, io_contract_ref=io_contract_ref),
            }
        ],
    )
    with pytest.raises(ValueError, match="capability_package_identity_conflict"):
        await repo.apply_patch(
            run.run_id,
            first.draft_version,
            first.draft_digest,
            "materialize-package-conflict-second",
            [
                {
                    "kind": "materialize_capability_package_candidate",
                    "value": process_package_value(repo, package_id="same-coordinate", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=second_program_ref, io_contract_ref=io_contract_ref),
                }
            ],
        )


@pytest.mark.asyncio
async def test_same_package_coordinate_across_runs_resolves_by_digest():
    repo = ObserverRepository()
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    packages = []
    for run_id in ("run-package-one", "run-package-two"):
        run = await repo.open_run(run_id, f"conversation-{run_id}", "check")
        await repo.apply_patch(
            run.run_id,
            run.draft_version,
            run.draft_digest,
            f"materialize-{run_id}",
            [
                {
                    "kind": "materialize_capability_package_candidate",
                        "value": process_package_value(repo, package_id="cross-run-coordinate", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref),
                }
            ],
        )
        packages.append((await repo.get_run(run.run_id)).capability_packages[0])

    first, second = packages
    assert first.version_ref == second.version_ref
    assert first.package_digest == second.package_digest
    first_ref = ResourceRef(resource_id=first.version_ref, version_or_digest=first.package_digest)
    second_ref = ResourceRef(resource_id=second.version_ref, version_or_digest=second.package_digest)
    assert (await repo.get_capability_package(first_ref)).package_digest == first.package_digest
    assert (await repo.get_capability_package(second_ref)).package_digest == second.package_digest


@pytest.mark.asyncio
async def test_package_lookup_rejects_legacy_coordinate_aliases() -> None:
    repo = ObserverRepository()
    run = await repo.open_run("run-canonical-ref", "conversation-canonical-ref", "check")
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-canonical-ref",
        [
            {
                "kind": "materialize_capability_package_candidate",
                "value": process_package_value(
                    repo,
                    package_id="canonical-package",
                    action_ref="grip://loom.local/workspaces/workspace-default/actions/check",
                    program_content_ref=program_ref,
                    io_contract_ref=io_contract_ref,
                ),
            }
        ],
    )
    package = (await repo.get_run(run.run_id)).capability_packages[0]

    assert await repo.get_capability_package(package.version_ref) == package
    for alias in (
        package.package_id,
        package.package_version,
        f"{package.package_id}:{package.package_version}",
        package.package_digest,
    ):
        with pytest.raises(KeyError):
            await repo.get_capability_package(alias)


@pytest.mark.asyncio
async def test_promoting_different_digest_same_reusable_coordinate_is_rejected():
    repo = ObserverRepository()
    io_contract_ref = await _empty_io_contract_ref(repo)
    candidates = []
    for run_id, source in (("run-promote-one", "print(1)"), ("run-promote-two", "print(2)")):
        run = await repo.open_run(run_id, f"conversation-{run_id}", "check")
        program_ref = await repo.put_content(source, media_type="text/x-python")
        await repo.apply_patch(
            run.run_id,
            run.draft_version,
            run.draft_digest,
            f"materialize-{run_id}",
            [
                {
                    "kind": "materialize_capability_package_candidate",
                        "value": process_package_value(repo, package_id="promote-coordinate", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref),
                }
            ],
        )
        await repo.fail_run(run.run_id, "test")
        candidates.append((await repo.get_run(run.run_id)).capability_packages[0])

    first, second = candidates
    first_ref = ResourceRef(resource_id=first.version_ref, version_or_digest=first.package_digest)
    second_ref = ResourceRef(resource_id=second.version_ref, version_or_digest=second.package_digest)
    await repo.promote_capability_package(first_ref, approved_digest=first.package_digest)
    with pytest.raises(ValueError, match="capability_package_identity_conflict"):
        await repo.promote_capability_package(second_ref, approved_digest=second.package_digest)


@pytest.mark.asyncio
async def test_promotion_derives_reusable_version_without_mutating_candidate():
    repo = ObserverRepository()
    run = await repo.open_run("run-promote", "conversation-promote", "check")
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    await repo.apply_patch(
        run.run_id, run.draft_version, run.draft_digest, "materialize-promote",
        [{"kind": "materialize_capability_package_candidate", "value": process_package_value(repo, package_id="pkg-promote", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref)}],
    )
    await repo.fail_run(run.run_id, "test")
    candidate = (await repo.get_run(run.run_id)).capability_packages[0]
    with TestClient(create_embedded_app(repo)) as client:
        listed = client.get(f"/api/v1/runs/{run.run_id}/capability-packages").json()
        assert listed[0]["resource"] == candidate.version_ref
        response = client.post(f"/api/v1/capability-packages/{listed[0]['resource']}/promote",
                               json={"approved_digest": candidate.package_digest})
        assert response.status_code == 200, response.text
        promoted = response.json()
        assert promoted["resource"] != candidate.version_ref
        assert promoted["resource"] in [r["uri"] for r in resources(client, "capability-package")]
        assert candidate.publication_state == "candidate"
        abandoned = client.post(f"/api/v1/capability-packages/{candidate.version_ref}/abandon")
        assert abandoned.status_code == 200, abandoned.text
        assert abandoned.json()["resource"] == candidate.version_ref
    reusable = await repo.get_capability_package(promoted["resource"])
    assert candidate.scope == "run_bound"
    assert reusable.scope == "workspace_reusable"
    assert reusable.publication_state == "published"
    assert reusable.package_version != candidate.package_version


@pytest.mark.asyncio
async def test_candidate_ref_promotion_is_idempotent():
    repo = ObserverRepository()
    run = await repo.open_run("run-promote-idempotent", "conversation-promote-idempotent", "check")
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-promote-idempotent",
        [{"kind": "materialize_capability_package_candidate", "value": process_package_value(repo, package_id="pkg-idempotent", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref)}],
    )
    await repo.fail_run(run.run_id, "test")
    candidate = (await repo.get_run(run.run_id)).capability_packages[0]

    first = await repo.promote_capability_package(candidate.version_ref, approved_digest=candidate.package_digest)
    second = await repo.promote_capability_package(candidate.version_ref, approved_digest=candidate.package_digest)
    third = await repo.promote_capability_package(candidate.version_ref, approved_digest=candidate.package_digest)

    assert second.version_ref == first.version_ref
    assert third.version_ref == first.version_ref
    published = [
        package
        for package in await repo.list_capability_packages(run_id=run.run_id, include_abandoned=True)
        if package.scope == "workspace_reusable" and package.publication_state == "published"
    ]
    assert len(published) == 1


@pytest.mark.asyncio
async def test_candidate_ref_promotion_is_idempotent_under_concurrent_retries():
    repo = ObserverRepository()
    run = await repo.open_run("run-promote-concurrent", "conversation-promote-concurrent", "check")
    program_ref = await _program_ref(repo)
    io_contract_ref = await _empty_io_contract_ref(repo)
    await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-promote-concurrent",
        [{"kind": "materialize_capability_package_candidate", "value": process_package_value(repo, package_id="pkg-concurrent", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref)}],
    )
    await repo.fail_run(run.run_id, "test")
    candidate = (await repo.get_run(run.run_id)).capability_packages[0]

    results = await asyncio.gather(*(
        repo.promote_capability_package(candidate.version_ref, approved_digest=candidate.package_digest)
        for _ in range(3)
    ))

    assert {result.version_ref for result in results} == {f"grip://loom.local/workspaces/workspace-default/packages/{candidate.package_id}/{candidate.package_version}-reusable"}
    published = [
        package
        for package in await repo.list_capability_packages(run_id=run.run_id, include_abandoned=True)
        if package.scope == "workspace_reusable" and package.publication_state == "published"
    ]
    assert len(published) == 1


@pytest.mark.asyncio
async def test_materialization_requires_io_contract_and_preuploaded_program_ref() -> None:
    repo = ObserverRepository()
    run = await repo.open_run("run-package-contract-required", "conversation-package-contract-required", "check")
    program_ref = await repo.put_content("print(1)", media_type="text/x-python")
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
    descriptor_ref = register_test_descriptor(repo, "grip://loom.local/workspaces/workspace-default/actions/check")

    with pytest.raises(ValueError, match="io_contract_ref"):
        await repo.apply_patch(
            run.run_id,
            run.draft_version,
            run.draft_digest,
            "materialize-contract-required",
            [
                {
                    "kind": "materialize_capability_package_candidate",
                    "value": {
                        "package_id": "pkg-contract-required",
                        "package_type": "function",
                        "execution": {"kind": "process:json_stdio", "version": "1"},
                        "action_exports": [{
                            "action_descriptor_ref": descriptor_ref.model_dump(mode="json"),
                            "effect_class": "Sandboxed",
                            "permissions": [],
                            "replay_safety": "DeclaredByPackage",
                            "runtime_binding": {},
                        }],
                        "body": {"program_content_ref": program_ref.model_dump(mode="json")},
                    },
                }
            ],
        )

    run = await repo.get_run(run.run_id)
    with pytest.raises(ValueError, match="package_contract_invalid"):
        await repo.apply_patch(
            run.run_id,
            run.draft_version,
            run.draft_digest,
            "materialize-inline-program",
            [
                {
                    "kind": "materialize_capability_package_candidate",
                    "value": {
                        "package_id": "pkg-inline-program",
                        "package_type": "function",
                        "execution": {"kind": "process:json_stdio", "version": "1"},
                        "action_exports": [action_export(descriptor_ref, contract_ref)],
                        "body": {"program": "print(1)"},
                    },
                }
            ],
        )


@pytest.mark.asyncio
async def test_observer_materializes_generic_http_service_envelope() -> None:
    repo = ObserverRepository()
    run = await repo.open_run(
        "run-http-package", "conversation-http-package", "serve echo"
    )
    io_contract_ref = await _empty_io_contract_ref(repo)
    descriptor_ref = register_test_descriptor(repo, "grip://loom.local/workspaces/workspace-default/actions/http/echo")

    await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-http-package",
        [
            {
                "kind": "materialize_capability_package_candidate",
                "value": {
                    "package_id": "pkg-http-service",
                    "package_type": "service",
                    "execution": {"kind": "container:http", "version": "1"},
                    "action_exports": [
                        action_export(
                            descriptor_ref,
                            io_contract_ref,
                            effect_class="NetworkService",
                            runtime_binding={"path": "/v1/echo"},
                        )
                    ],
                    "body": {
                        "image_ref": "registry.example/loom/echo@sha256:"
                        + "d" * 64,
                        "container_port": 8080,
                        "health_path": "/healthz",
                    },
                },
            }
        ],
    )

    package = (await repo.get_run(run.run_id)).capability_packages[0]
    assert package.package_type == "service"
    assert package.action_exports[0].runtime_binding == {"path": "/v1/echo"}


@pytest.mark.asyncio
async def test_observer_rejects_unresolvable_action_descriptor() -> None:
    repo = ObserverRepository()
    run = await repo.open_run(
        "run-missing-descriptor", "conversation-missing-descriptor", "reject"
    )
    io_contract_ref = await _empty_io_contract_ref(repo)
    missing_descriptor = ResourceRef(
        resource_id="grip://loom.local/workspaces/workspace-default/actions/missing/descriptor",
        version_or_digest="a" * 64,
        identity_criterion="descriptor_digest",
    )

    with pytest.raises(ValueError, match="action_descriptor_unavailable"):
        await repo.apply_patch(
            run.run_id,
            run.draft_version,
            run.draft_digest,
            "materialize-missing-descriptor",
            [
                {
                    "kind": "materialize_capability_package_candidate",
                    "value": {
                        "package_id": "pkg-missing-descriptor",
                        "package_type": "service",
                        "execution": {"kind": "container:http", "version": "1"},
                        "action_exports": [
                            action_export(
                                missing_descriptor,
                                io_contract_ref,
                                runtime_binding={"path": "/v1/echo"},
                            )
                        ],
                        "body": {
                            "image_ref": "registry.example/loom/echo@sha256:"
                            + "d" * 64,
                            "container_port": 8080,
                            "health_path": "/healthz",
                        },
                    },
                }
            ],
        )


@pytest.mark.asyncio
async def test_binding_package_with_different_io_contract_is_not_ready() -> None:
    repo = ObserverRepository()
    seed_embedded_slaves(repo)
    schema_ref = await repo.put_content({"type": "object"}, media_type="application/schema+json")
    closure_contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": schema_ref.model_dump(mode="json"),
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    other_schema_ref = await repo.put_content({"type": "array"}, media_type="application/schema+json")
    io_contract_ref = await repo.put_content(
        {
            "schema_version": "io.v1",
            "input_schema_ref": other_schema_ref.model_dump(mode="json"),
            "output_schema_ref": None,
            "success_semantics": None,
            "success_validator_ref": None,
        },
        media_type="application/vnd.loom.io-contract+json",
    )
    program_ref = await repo.put_content("print(1)", media_type="text/x-python")
    closure = ClosureContract(
        closure_id="closure-package-contract",
        goal="check",
        body=TaskClosure(
            closure_id="closure-package-contract",
            program={"action_ref": "grip://loom.local/workspaces/workspace-default/actions/check", "io_contract_ref": closure_contract_ref.model_dump(mode="json")},
        ),
    )
    run = await repo.open_run("run-package-contract", "conversation-package-contract", "check", closure_contract=closure)
    materialized = await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "materialize-package-contract",
        [
            {"kind": "add_typed_hole", "value": {"hole_id": "h_check"}},
            {
                "kind": "materialize_capability_package_candidate",
                "value": process_package_value(repo, package_id="pkg-contract-mismatch", action_ref="grip://loom.local/workspaces/workspace-default/actions/check", program_content_ref=program_ref, io_contract_ref=io_contract_ref),
            },
        ],
    )
    package = (await repo.get_run(run.run_id)).capability_packages[0]
    bound = await repo.apply_patch(
        run.run_id,
        materialized.draft_version,
        materialized.draft_digest,
        "bind-package-contract",
        [
            {
                "kind": "bind_compute_hole",
                "value": {
                    "binding_id": "binding-contract-mismatch",
                    "hole_id": "h_check",
                    "action_descriptor_ref": package.action_exports[0].action_descriptor_ref.model_dump(mode="json"),
                    "capability_package_ref": {"resource_id": package.version_ref},
                    "target_resource_ref": {"resource_id": "grip://loom.local/workspaces/workspace-default/slaves/slave-a"},
                },
            }
        ],
    )

    assert any(blocker["code"] == "io_contract_mismatch" for blocker in bound.readiness["blockers"])
