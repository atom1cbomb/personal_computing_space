import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from loom_orchestration import LoomClient, OrchestratorPackage, SubmissionBuilder, ResourceRef as PublicResourceRef
from loom_v2.contracts.types import CapabilityHealthReport, NodeIntent, ResourceRef
from loom_v2.driver.orchestration_runtime import DynamicOrchestrationRuntime
from loom_v2.driver.remote_repository import RemoteObserverRepository
from loom_v2.driver.orchestrator import OrchestrationActivationWait
from loom_v2.driver.worker import WorkerSession
from loom_v2.observer.app import create_app
from loom_v2.observer.cleanup import continuation, registrations
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.app import create_app as create_slave_app
from loom_v2.testing.observer import seed_embedded_slaves
from tests.api.test_dynamic_nodes import _register_slave
from tests.driver.test_remote_service import RepositoryControl


async def lifecycle_run(repo, *, max_nodes=4, register=True):
    seed_embedded_slaves(repo)
    if repo.sessions is not None:
        await _register_slave(repo, "slave-a", "slave-a-instance")
    settings = Settings(grip_token="cleanup-test", public_base_url="http://observer")
    async with LoomClient("http://observer", token="cleanup-test", allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=create_app(repo, settings=settings))) as client:
        contract = await client.upload_content({"schema_version": "io.v1"}, media_type="application/vnd.loom.io-contract+json")
        descriptor = await client.upload_content({"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/echo", "name": "echo"}, media_type="application/vnd.loom.action-descriptor+json")
        source = await client.upload_content("import json,sys; print(json.dumps(json.load(sys.stdin)))", media_type="text/x-python")
        package = await client.register_package({
            "package_id": "lifecycle-echo", "package_version": "v1", "package_type": "function",
            "execution": {"kind": "process:json_stdio", "version": "1"},
            "body": {"program_content_ref": source.model_dump(mode="json")},
            "action_exports": [{
                "action_descriptor_ref": descriptor.model_dump(mode="json"), "io_contract_ref": contract.model_dump(mode="json"),
                "effect_class": "Sandboxed", "permissions": [], "replay_safety": "DeterministicByEventLog", "runtime_binding": {},
            }],
        })
        node_ref = ResourceRef(resource_id=package["resource"], version_or_digest=package["package_digest"])
        program = await client.upload_content('async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":\n    return input_ref\n', media_type="text/x-python")
        orchestrator_descriptor = await client.upload_content({"schema_version": "action.v1", "resource_id": "grip://loom.local/workspaces/workspace-default/actions/orchestrate", "name": "orchestrate"}, media_type="application/vnd.loom.action-descriptor+json")
        orchestrator = await client.register_package(OrchestratorPackage(
            program_content_ref=program, allowed_node_package_refs=[PublicResourceRef.model_validate(node_ref.model_dump(mode="json"))],
            action_descriptor_ref=orchestrator_descriptor, io_contract_ref=contract, max_nodes=max_nodes, max_live_nodes=1,
        ).manifest())
        input_ref = await client.upload_content({"ok": True})
        plan = SubmissionBuilder(
            goal="lifecycle", body={"program": {"action_ref": "grip://loom.local/workspaces/workspace-default/actions/orchestrate", "io_contract_ref": contract.model_dump(mode="json")}},
            orchestrator_package={"resource_id": orchestrator["resource"], "version_or_digest": orchestrator["package_digest"]},
            input_binding=input_ref, resource_budget={"execution_timeout_seconds": 30, "cleanup_timeout_seconds": 5},
        ).build(request_id="lifecycle")
        submission = await client.submit(request_id="lifecycle", plan=plan)
    claimed = await claim(repo)
    assert claimed["run"]["run_id"] == submission["run_id"]
    record = await repo.get_run(submission["run_id"])
    if not register:
        return record, node_ref, None
    resource = await repo.register_program_resource(record.run_id, execution_id=record.execution_id, step_id="resource", mcue_ref="grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1", identity="vm-test", package_ref=node_ref)
    await repo.register_cleanup(record.run_id, execution_id=record.execution_id, step_id="delete", resource_ref=resource,
                                cleanup_action_ref=ResourceRef.model_validate(descriptor.model_dump(mode="json")), input_mapping={"id": "$resource.identity"})
    return record, node_ref, resource


async def claim(repo, epoch=1):
    return await repo.claim_next_execution(workspace_id="workspace-default", driver_id="driver", driver_instance_id="instance", driver_epoch=epoch)


def runtime(repo):
    return DynamicOrchestrationRuntime(repository=repo, executor=None, workers={
        "slave-a": WorkerSession("slave-a", "http://slave-a", transport=httpx.ASGITransport(app=create_slave_app("slave-a"))),
    })


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_registered_cleanup_executes_and_preserves_terminal(terminal):
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo)
    if terminal == "completed":
        result_ref = await repo.materialize_program_json(record.run_id, execution_id=record.execution_id, step_id="final", value={"ok": True})
        node = await repo.accept_node_intent(record.run_id, NodeIntent(intent_id="use", execution_id=record.execution_id,
            package_ref=package, action_descriptor_ref=ResourceRef.model_validate(registrations(record)[0]["action_descriptor_ref"]), input_refs=[result_ref]), selected_target="slave-a")
        result_ref = await runtime(repo)._execute_node(record.run_id, node, "slave-a")
        await repo.complete_orchestration(record.run_id, result_ref)
    elif terminal == "failed":
        await repo.fail_run(record.run_id, {"code": "program_failure"})
    else:
        await repo.cancel_run(record.run_id)
    claimed = await claim(repo)
    assert claimed["run"]["state"] == "cleaning_up"
    finished = await runtime(repo).run_cleanup(record.run_id)
    assert finished.state == terminal
    assert finished.outcome["cleanup"] == {"state": "completed", "errors": []}
    assert finished.attempts[-1]["value"] == {"id": "vm-test"}
    assert registrations(finished)[0]["arguments"]["resource_ref"] == resource.model_dump(mode="json")
    assert await claim(repo) is None


async def test_cleanup_registration_is_idempotent_and_reserves_nodes():
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo, max_nodes=1)
    registered = registrations(record)[0]
    arguments = dict(execution_id=record.execution_id, step_id="delete", resource_ref=resource,
                     cleanup_action_ref=ResourceRef.model_validate(registered["action_descriptor_ref"]), input_mapping={"id": "$resource.identity"})
    assert await repo.register_cleanup(record.run_id, **arguments) == registered
    with pytest.raises(ValueError, match="program_step_conflict"):
        await repo.register_cleanup(record.run_id, **{**arguments, "input_mapping": {"id": {"constant": "other"}}})
    with pytest.raises(ValueError, match="cleanup_already_registered"):
        await repo.register_cleanup(record.run_id, **{**arguments, "step_id": "duplicate"})
    input_ref = await repo.materialize_program_json(record.run_id, execution_id=record.execution_id, step_id="input", value={"id": "vm-test"})
    with pytest.raises(ValueError, match="orchestration_node_limit_exceeded"):
        await repo.accept_node_intent(record.run_id, NodeIntent(intent_id="extra", execution_id=record.execution_id, package_ref=package,
            action_descriptor_ref=ResourceRef.model_validate(registered["action_descriptor_ref"]), input_refs=[input_ref]), selected_target="slave-a")
    with pytest.raises(ValueError, match="program_step_conflict"):
        await repo.materialize_program_json(record.run_id, execution_id=record.execution_id, step_id="input", value={"different": True})


async def test_cleanup_deadline_reports_remaining_resource():
    repo = ObserverRepository()
    record, _, resource = await lifecycle_run(repo)
    await repo.cancel_run(record.run_id)
    continuation(record)["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await claim(repo)
    finished = await runtime(repo).run_cleanup(record.run_id)
    assert finished.state == "cancelled"
    assert finished.outcome["cleanup"]["state"] == "failed"
    assert any(error.get("resource_ref") == resource.model_dump(mode="json") for error in finished.outcome["cleanup"]["errors"])


async def test_public_cancel_by_run_id_starts_cleanup():
    repo = ObserverRepository()
    record, _, _ = await lifecycle_run(repo)
    settings = Settings(grip_token="cleanup-test", public_base_url="http://observer")
    async with LoomClient("http://observer", token="cleanup-test", allow_insecure_grip=True,
                          transport=httpx.ASGITransport(app=create_app(repo, settings=settings))) as client:
        cancelled = await client.cancel(record.run_id)
    assert cancelled["state"] == "cleaning_up"


async def test_cleanup_resumes_after_repository_restart_without_reexecution(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'observer.db'}")
    repo = ObserverRepository(engine)
    await repo.init_db()
    record, _, _ = await lifecycle_run(repo)
    await repo.cancel_run(record.run_id)
    await claim(repo)
    executor = runtime(repo)
    original_finish = repo.finish_cleanup

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError()

    repo.finish_cleanup = crash
    with pytest.raises(asyncio.CancelledError):
        await executor.run_cleanup(record.run_id)
    repo.finish_cleanup = original_finish
    restored = ObserverRepository(engine, content_store=repo.content_store)
    await restored.init_db()
    await restored.recover_execution_requests_for_driver("workspace-default", driver_id="driver", driver_instance_id="instance", driver_epoch=2)
    claimed = await claim(restored, epoch=2)
    assert claimed["run"]["state"] == "cleaning_up"
    finished = await runtime(restored).run_cleanup(record.run_id)
    assert finished.outcome["cleanup"]["state"] == "completed"
    assert len(finished.dynamic_nodes) == 1
    assert len([attempt for attempt in finished.attempts if attempt.get("node_id")]) == 1
    await engine.dispose()


async def test_activation_registration_replay_keeps_target_and_rejects_changed_arguments():
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo)
    arguments = dict(execution_id=record.execution_id, step_id="access", package_ref=package, target_resource_ref=resource,
                     target_slave="slave-a", instance_name="vm-test", instance_config={}, credential_profile="ssh-account-a", requires_authentication=True)
    first = await repo.ensure_program_activation(record.run_id, **arguments)
    replayed = await repo.ensure_program_activation(record.run_id, **{**arguments, "target_slave": "slave-b"})
    assert replayed == first
    saved, = await repo.list_resource_activations("workspace-default", resource)
    assert saved.credential_profile == "ssh-account-a"
    with pytest.raises(ValueError, match="program_step_conflict"):
        await repo.ensure_program_activation(record.run_id, **{**arguments, "credential_profile": "ssh-account-b"})
    with pytest.raises(ValueError, match="program_step_conflict"):
        await repo.ensure_program_activation(record.run_id, **{**arguments, "instance_config": {"host": "other"}})
    assert len(await repo.list_resource_activations("workspace-default", resource)) == 1


async def test_workspace_activation_writes_survive_concurrent_discovery(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'observer.db'}")
    repo = ObserverRepository(engine)
    await repo.init_db()
    record, package_ref, resource = await lifecycle_run(repo)
    original_persist = repo._persist_workspace_package

    async def concurrent_discovery(package, **values):
        await repo._load_workspace_packages()
        await original_persist(package, **values)

    monkeypatch.setattr(repo, "_persist_workspace_package", concurrent_discovery)

    async def activate(instance):
        return await repo.set_capability_desired(package_ref, "slave-a", desired_state="running", idempotency_key=instance,
            run_id=record.run_id, instance_name=instance, target_resource_ref=resource, instance_config={})

    activations = await asyncio.gather(activate("first"), activate("second"))
    await asyncio.gather(*[
        repo.record_capability_health(CapabilityHealthReport(
            report_id=f"health-{activation.instance_name}", package_version_ref=activation.package_version_ref,
            package_digest=activation.package_digest, target_slave="slave-a", instance_name=activation.instance_name,
            activation_revision=activation.activation_revision, activation_state="ready",
        )) for activation in activations
    ])
    restored = ObserverRepository(engine, content_store=repo.content_store)
    await restored.init_db()
    stored = await restored.list_resource_activations("workspace-default", resource)
    assert [(item.instance_name, item.activation_state) for item in stored] == [("first", "ready"), ("second", "ready")]
    await engine.dispose()


async def test_auth_wait_does_not_cleanup_until_deadline():
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo)
    activation = await repo.ensure_program_activation(record.run_id, execution_id=record.execution_id, step_id="access", package_ref=package,
        target_resource_ref=resource, target_slave="slave-a", instance_name="vm-test", instance_config={}, requires_authentication=True)
    await repo.activation_wait(record.run_id, step_id="auth", package_ref=package, activation_ref=activation)
    assert (await repo.get_run(record.run_id)).state == "awaiting_auth"
    assert continuation(record) is None
    assert await claim(repo) is None
    request = await repo.get_execution_request("workspace-default", "lifecycle")
    request.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await repo._persist_execution_request(request)
    claimed = await claim(repo)
    assert claimed["run"]["state"] == "cleaning_up"
    finished = await runtime(repo).run_cleanup(record.run_id)
    assert finished.state == "failed"
    assert finished.outcome["terminal_error"]["code"] == "execution_deadline_exceeded"
    assert finished.outcome["cleanup"]["state"] == "completed"


async def test_auth_completed_before_wait_registration_is_not_lost():
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo)
    activation = await repo.ensure_program_activation(record.run_id, execution_id=record.execution_id, step_id="access", package_ref=package,
        target_resource_ref=resource, target_slave="slave-a", instance_name="vm-test", instance_config={}, requires_authentication=True)
    ready = await repo.record_capability_health(CapabilityHealthReport(report_id="auth-before-wait", package_version_ref=package.resource_id,
        package_digest=package.digest, target_slave="slave-a", instance_name="vm-test",
        activation_revision=activation.access_binding["activation_revision"], activation_state="ready"))
    await repo.activation_wait(record.run_id, step_id="auth", package_ref=package, activation_ref=activation)
    reclaimed = await claim(repo)
    assert reclaimed["run"]["state"] == "running"
    assert reclaimed["run"]["execution_id"] == record.execution_id
    assert continuation(await repo.get_run(record.run_id)) is None
    second = await repo.ensure_program_activation(record.run_id, execution_id=record.execution_id, step_id="second-access", package_ref=package,
        target_resource_ref=resource, target_slave="slave-a", instance_name="vm-test-second", instance_config={}, requires_authentication=True)
    await repo.activation_wait(record.run_id, step_id="second-auth", package_ref=package, activation_ref=second)
    assert await repo.recover_activation_wait(ready) == []
    assert (await repo.get_run(record.run_id)).state == "awaiting_auth"


async def test_unknown_main_attempt_does_not_blindly_cleanup():
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo)
    event = registrations(record)[0]
    input_ref = await repo.materialize_program_json(record.run_id, execution_id=record.execution_id, step_id="use", value={"id": "vm-test"})
    node = await repo.accept_node_intent(record.run_id, NodeIntent(intent_id="use", execution_id=record.execution_id, package_ref=package,
        action_descriptor_ref=ResourceRef.model_validate(event["action_descriptor_ref"]), input_refs=[input_ref]), selected_target="slave-a")
    await repo.dispatch_dynamic_node(record.run_id, node.node_id, target="slave-a")
    await repo.cancel_run(record.run_id)
    await claim(repo)
    finished = await runtime(repo).run_cleanup(record.run_id)
    assert len(finished.dynamic_nodes) == 1
    errors = finished.outcome["cleanup"]["errors"]
    assert any(error["code"] == "execution_recovery_required" for error in errors)
    assert any(error.get("resource_ref") == resource.model_dump(mode="json") for error in errors)


async def test_remote_host_commands_and_cleanup_owner_fencing():
    repo = ObserverRepository()
    record, package, resource = await lifecycle_run(repo)
    control = RepositoryControl(repo)
    await control.register()
    remote = RemoteObserverRepository(control, content_store=repo.content_store)
    ref = await remote.materialize_program_json(record.run_id, execution_id=record.execution_id, step_id="remote-input", value={"id": "vm-test"})
    assert await repo._load_json_content(ref) == {"id": "vm-test"}
    activation = await remote.ensure_program_activation(record.run_id, execution_id=record.execution_id, step_id="remote-access", package_ref=package,
        target_resource_ref=resource, target_slave="slave-a", instance_name="vm-test", instance_config={}, requires_authentication=False,
        credential_profile="remote-account")
    activations = await remote.list_package_activations(await remote.get_capability_package(package))
    assert activations[0].activation_revision == activation.access_binding["activation_revision"]
    assert activations[0].credential_profile == "remote-account"
    await repo.cancel_run(record.run_id)
    with pytest.raises(ValueError, match="stale_execution_owner"):
        await remote.finish_cleanup(record.run_id, execution_id=record.execution_id, errors=[])
    await repo.claim_next_execution(workspace_id=control.workspace_id, driver_id=control.driver_id,
        driver_instance_id=control.instance_id, driver_epoch=control.driver_epoch)
    finished = await remote.finish_cleanup(record.run_id, execution_id=record.execution_id, errors=[])
    assert finished.state == "cancelled"
    assert finished.outcome["cleanup"]["errors"][0]["code"] == "cleanup_unconfirmed"


async def test_one_run_create_authenticate_use_cleanup_without_duplicate_create(monkeypatch):
    repo = ObserverRepository()
    record, package_ref, _ = await lifecycle_run(repo, register=False)
    package = await repo.get_capability_package(package_ref)
    descriptor = package.action_exports[0].action_descriptor_ref
    executor = runtime(repo)

    provisions = []
    worker = executor.workers["slave-a"]
    original_provision = worker.provision

    async def provision(**kwargs):
        report = await original_provision(**kwargs)
        command = kwargs["command"]
        if command.instance_name == "vm-test":
            provisions.append(command)
            if len(provisions) == 1:
                return report.model_copy(update={"activation_state": "pending_auth"})
        return report

    monkeypatch.setattr(worker, "provision", provision)

    async def health(state):
        activation, = await repo.list_resource_activations("workspace-default", "grip://loom.local/workspaces/workspace-default/resources/computing/vm/vm-test")
        return await repo.record_capability_health(CapabilityHealthReport(
            report_id=f"health-{state}", package_version_ref=package.version_ref, package_digest=package.package_digest,
            target_slave=activation.target_slave, instance_name=activation.instance_name,
            activation_revision=activation.activation_revision, activation_state=state,
            details={"runtime_profile": {"authentication": {"status": "authenticated" if state == "ready" else "auth_required"}}},
        ))

    class LifecycleProgram:
        async def run(self, program, input_ref, **context):
            created = await context["emit_node"]("create", package_ref, descriptor, [input_ref])
            await context["result"](created)
            resource = await context["register_resource"](step_id="resource", mcue_ref="grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1", identity="vm-test", package_ref=package_ref)
            await context["defer_cleanup"](step_id="delete", resource_ref=resource, cleanup_action_ref=descriptor, input_mapping={"id": "$resource.identity"})
            activation = await context["ensure_activation"](step_id="access", package_ref=package_ref, target_resource_ref=resource,
                instance_name="vm-test", instance_config={"host": "192.0.2.1", "port": 22, "user": "root"}, requires_authentication=True)
            await context["wait_activation"](step_id="auth", activation_ref=activation, ready_state="authenticated")
            dynamic_input = await context["materialize_json"](step_id="use-input", value={"id": "vm-test", "ok": True})
            handle = await context["emit_node"]("use", package_ref, descriptor, [dynamic_input], target_resource_ref=resource, activation_ref=activation)
            return await context["result"](handle)

    executor.executor = LifecycleProgram()
    with pytest.raises(OrchestrationActivationWait):
        await executor.run(record.run_id)
    waiting = await repo.get_run(record.run_id)
    assert waiting.state == "awaiting_auth"
    assert waiting.outcome is None
    assert continuation(waiting) is None
    assert len(provisions) == 1
    assert provisions[0].target_resource_ref.resource_id == "grip://loom.local/workspaces/workspace-default/resources/computing/vm/vm-test"
    assert provisions[0].instance_config == {"host": "192.0.2.1", "port": 22, "user": "root"}
    original_execution = waiting.execution_id
    await health("ready")
    claimed = await claim(repo)
    assert claimed["run"]["execution_id"] == original_execution
    await executor.run(record.run_id)
    await claim(repo)
    finished = await executor.run_cleanup(record.run_id)
    assert finished.state == "completed"
    assert finished.outcome["cleanup"]["state"] == "completed"
    assert [node.intent_id for node in finished.dynamic_nodes] == ["create", "use", "cleanup:delete"]
    assert finished.dynamic_nodes[1].instance_name == "vm-test"
    access = executor.workers["slave-a"].transport.app.state.service.activations[package.version_ref + "#vm-test"]
    assert access.target_resource_ref == finished.dynamic_nodes[1].target_resource_ref
    assert access.instance_config == {"host": "192.0.2.1", "port": 22, "user": "root"}
    assert len([attempt for attempt in finished.attempts if attempt.get("node_id")]) == 3
    assert len(provisions) == 2
