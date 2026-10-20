from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loom_v2.contracts.types import (
    CapabilityPackageVersion,
    CapabilityProvisionCommand,
    ComputeBinding,
    DynamicNode,
    NodeInputBinding,
    NodeIntent,
    ResourceRef,
    TaskClosure,
)
from loom_v2.contracts.refs import input_binding_for, action_name, package_body_ref, single_export
from loom_v2.contracts.package_contracts import DRIVER_ORCHESTRATOR_KEY
from loom_v2.observer.repository import ObserverRepository
from loom_v2.driver.worker import WorkerSession, WorkerUnavailableError
from loom_v2.slave.executor import ExecutionResult
from loom_v2.slave.service import SlaveService

from .orchestrator import DockerOrchestrationExecutor, OrchestrationActivationWait, OrchestrationExecutorError, OrchestrationProgramError


@dataclass
class _PendingNode:
    intent_id: str
    task: asyncio.Task[ResourceRef]


class DynamicOrchestrationRuntime:
    """Run a committed orchestration package and its dynamic child nodes."""

    def __init__(
        self,
        *,
        repository: ObserverRepository,
        executor: DockerOrchestrationExecutor,
        slaves: dict[str, SlaveService] | None = None,
        workers: dict[str, WorkerSession] | None = None,
        driver_id: str | None = None,
        driver_epoch: int | None = None,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.slaves = slaves or {}
        self.workers = workers or {}
        self.driver_id = driver_id
        self.driver_epoch = driver_epoch
        self._round_robin_cursor: dict[str, int] = {}

    async def run(self, run_id: str) -> tuple[Any, ExecutionResult]:
        record = await self.repository.get_run(run_id)
        if record.execution_id is None or record.state != "running":
            raise RuntimeError("execution_not_running")
        snapshot = record.committed.snapshot if record.committed is not None else record.draft.snapshot
        orchestration_ref = snapshot.program_systems.package_ref
        if orchestration_ref is None or (
            snapshot.program_systems.execution.kind,
            snapshot.program_systems.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY[1:]:
            raise RuntimeError("orchestration_package_not_bound")
        try:
            orchestration_package = await self.repository.get_capability_package(orchestration_ref, run_id=run_id)
            if orchestration_package.publication_state == "abandoned":
                raise RuntimeError("capability_package_abandoned")
            if (
                orchestration_package.package_type,
                orchestration_package.execution.kind,
                orchestration_package.execution.version,
            ) != DRIVER_ORCHESTRATOR_KEY:
                raise RuntimeError("orchestration_package_invalid")
            orchestration_export = single_export(orchestration_package)
            if (
                orchestration_export.replay_safety != "DeterministicByEventLog"
                or bool(orchestration_package.body.get("captures_run_state", False))
                or orchestration_package.body.get("captured_secret_refs")
                or orchestration_package.body.get("captured_path_refs")
            ):
                raise RuntimeError("orchestration_not_replayable")
            if int(orchestration_package.body["max_live_nodes"]) > int(orchestration_package.body["max_nodes"]):
                raise RuntimeError("orchestration_live_node_limit_exceeded")
            program_ref = package_body_ref(orchestration_package, "program_content_ref")
            program = await self.repository.content_store.get(
                program_ref,
                expected_digest=program_ref.digest,
            )
            action_ref = snapshot.program.action_ref or snapshot.compute.action_ref
            input_binding = input_binding_for(snapshot, action_ref)
            if input_binding is None:
                raise RuntimeError("orchestration_input_missing")
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                current = await self.repository.get_run(run_id)
                if current.state == "running":
                    await self.repository.fail_run(run_id, {"code": str(exc) or "orchestration_setup_failed"})
            raise

        pending: dict[str, _PendingNode] = {}
        intent_sequence = 0

        async def read_json(ref: ResourceRef):
            return await self.repository._load_json_content(ref)

        async def materialize_json(*, step_id: str, value: Any) -> ResourceRef:
            return await self.repository.materialize_program_json(
                run_id, execution_id=record.execution_id, step_id=step_id, value=value,
            )

        async def register_resource(*, step_id: str, resource_type: str, identity: str, package_ref: ResourceRef, managed_by_ref: ResourceRef | None = None) -> ResourceRef:
            return await self.repository.register_program_resource(
                run_id, execution_id=record.execution_id, step_id=step_id,
                resource_type=resource_type, identity=identity, package_ref=package_ref,
                managed_by_ref=managed_by_ref,
            )

        async def ensure_activation(*, step_id: str, package_ref: ResourceRef, target_resource_ref: ResourceRef, instance_name: str = "default", instance_config: dict[str, Any] | None = None, requires_authentication: bool = False) -> ResourceRef:
            await self.repository.refresh_slaves()
            package = await self.repository.get_capability_package(package_ref, run_id=run_id)
            target = self._select_target(package, package.action_exports[0].action_descriptor_ref, record)
            activation_ref = await self.repository.ensure_program_activation(
                run_id, execution_id=record.execution_id, step_id=step_id,
                package_ref=package_ref, target_slave=target,
                instance_name=instance_name,
                target_resource_ref=target_resource_ref,
                instance_config=instance_config or {},
                requires_authentication=requires_authentication,
            )
            binding = activation_ref.access_binding
            activation = next((item for item in await self.repository.list_package_activations(package)
                               if item.target_slave == binding["target_slave"]
                               and item.instance_name == binding["instance_name"]
                               and item.activation_revision == binding["activation_revision"]), None)
            if activation is None:
                raise RuntimeError("activation_not_found")
            if activation.activation_state in {"provisioning", "not_installed"}:
                command = CapabilityProvisionCommand(
                    command_id=f"provision-{record.execution_id}-{step_id}",
                    package_version_ref=package.version_ref,
                    package_digest=package.package_digest,
                    target_slave=activation.target_slave,
                    workspace_id=record.closure_contract.workspace_id,
                    activation_closure_version_ref=activation.activation_closure_version_ref,
                    idempotency_key=activation.last_idempotency_key,
                    activation_revision=activation.activation_revision,
                    instance_name=activation.instance_name,
                    target_resource_ref=activation.target_resource_ref,
                    instance_config=dict(activation.instance_config),
                )
                worker = self.workers.get(activation.target_slave)
                slave = self.slaves.get(activation.target_slave)
                if worker is not None:
                    report = await worker.provision(command=command, package=package,
                                                    driver_id=self.driver_id, driver_epoch=self.driver_epoch)
                elif slave is not None:
                    report = await slave.provision(command, package)
                else:
                    raise RuntimeError("node_target_unavailable")
                await self.repository.record_capability_health(report, run_id=run_id)
            return activation_ref

        async def wait_activation(*, step_id: str, activation_ref: ResourceRef, ready_state: str = "ready") -> dict[str, Any]:
            package_ref = str(activation_ref.access_binding.get("package_version_ref") or "")
            target_slave = str(activation_ref.access_binding.get("target_slave") or "")
            instance_name = str(activation_ref.access_binding.get("instance_name") or "default")
            if not package_ref:
                raise RuntimeError("activation_ref_invalid")
            package = await self.repository.get_capability_package(package_ref, run_id=run_id)
            while True:
                activations = await self.repository.list_package_activations(package)
                activation = next((item for item in activations if item.package_version_ref == package.version_ref and item.package_digest == activation_ref.digest and item.target_slave == target_slave and item.instance_name == instance_name), None)
                if activation is None:
                    raise RuntimeError("activation_not_found")
                if activation.activation_state not in {"provisioning", "not_installed"} or activation.activation_revision != activation_ref.access_binding.get("activation_revision"):
                    break
                await asyncio.sleep(0.25)
            expected_revision = activation_ref.access_binding.get("activation_revision")
            if expected_revision is not None:
                try:
                    revision_matches = int(expected_revision) == activation.activation_revision
                except (TypeError, ValueError):
                    revision_matches = False
                if not revision_matches:
                    raise RuntimeError("activation_revision_conflict")
            expected_resource = activation_ref.access_binding.get("target_resource_ref")
            if expected_resource is not None and activation.target_resource_ref != ResourceRef.model_validate(expected_resource):
                raise RuntimeError("activation_resource_binding_conflict")
            if activation.activation_state == "pending_auth":
                raise OrchestrationActivationWait({
                    "step_id": step_id,
                    "package_ref": ResourceRef(resource_id=package.version_ref, version_or_digest=package.package_digest).model_dump(mode="json"),
                    "activation_ref": activation_ref.model_dump(mode="json"),
                    "target_slave": activation.target_slave,
                    "instance_name": activation.instance_name,
                    "activation_revision": activation.activation_revision,
                    "action": "authenticate",
                })
            if activation.activation_state != ready_state and not (ready_state == "authenticated" and activation.activation_state == "ready" and activation.runtime_profile.get("authentication", {}).get("status") == "authenticated"):
                raise RuntimeError("activation_not_ready")
            return activation.model_dump(mode="json")

        async def defer_cleanup(*, step_id: str, resource_ref: ResourceRef, cleanup_action_ref: ResourceRef, input_mapping: dict[str, Any]) -> None:
            await self.repository.register_cleanup(
                run_id, execution_id=record.execution_id, step_id=step_id,
                resource_ref=resource_ref, cleanup_action_ref=cleanup_action_ref,
                input_mapping=input_mapping,
            )

        async def emit_node(*args: Any, target_resource_ref: ResourceRef | None = None, activation_ref: ResourceRef | None = None) -> str:
            if len(args) == 3:
                step_id = ""
                package_ref, action_descriptor_ref, input_refs = args
            elif len(args) == 4:
                step_id, package_ref, action_descriptor_ref, input_refs = args
            else:
                raise RuntimeError("node_intent_arguments_invalid")
            nonlocal intent_sequence
            intent_sequence += 1
            intent_id = step_id or f"intent-{intent_sequence:04d}"
            try:
                node_package = await self.repository.get_capability_package(package_ref, run_id=run_id)
            except KeyError as exc:
                try:
                    candidate = await self.repository.get_capability_package(
                        ResourceRef(resource_id=package_ref.resource_id),
                        run_id=run_id,
                    )
                except KeyError:
                    try:
                        await self.repository.get_capability_package(package_ref)
                    except KeyError:
                        raise RuntimeError("node_package_not_found") from exc
                    raise RuntimeError("capability_package_scope_mismatch") from exc
                if package_ref.digest and package_ref.digest.lower() != candidate.package_digest.lower():
                    raise RuntimeError("node_package_digest_mismatch") from exc
                node_package = candidate
            existing = next((node for node in record.dynamic_nodes if node.intent_id == intent_id), None)
            if existing is not None and existing.state == "completed":
                if (
                    existing.package_ref != package_ref
                    or existing.action_descriptor_ref != action_descriptor_ref
                    or existing.input_refs != input_refs
                    or existing.target_resource_ref != target_resource_ref
                    or existing.activation_ref != activation_ref
                ):
                    raise RuntimeError("node_intent_conflict")
                current_record = await self.repository.get_run(run_id)
                previous = next(
                    (
                        attempt.get("result_ref")
                        for attempt in reversed(current_record.attempts)
                        if attempt.get("node_id") == existing.node_id and attempt.get("state") == "completed" and attempt.get("result_ref")
                    ),
                    None,
                )
                if previous is None:
                    raise RuntimeError("replay_result_missing")
                task = asyncio.create_task(asyncio.sleep(0, result=ResourceRef.model_validate(previous)))
                pending[intent_id] = _PendingNode(intent_id=intent_id, task=task)
                return intent_id
            previous_event = next(
                (
                    event
                    for event in reversed(record.events)
                    if event.get("phase") in {"node_accepted", "node_reassigned"}
                    and event.get("node_id") == existing.node_id
                ),
                None,
            ) if existing is not None else None
            previous_target = (
                (
                    previous_event.get("selected_target")
                    or previous_event.get("to_target")
                    or previous_event.get("target")
                )
                if previous_event is not None
                else None
            )
            await self.repository.refresh_slaves()
            current_record = await self.repository.get_run(run_id)
            bound_target = str((activation_ref.access_binding if activation_ref else {}).get("target_slave") or "")
            if bound_target:
                target = bound_target
            elif existing is not None and previous_target and not record.allow_reassignment:
                target = previous_target
            else:
                target = self._select_target(node_package, action_descriptor_ref, current_record)
            intent = NodeIntent(
                intent_id=intent_id,
                execution_id=record.execution_id,
                package_ref=package_ref,
                action_descriptor_ref=action_descriptor_ref,
                input_refs=input_refs,
                instance_name=str(activation_ref.access_binding.get("instance_name") or "default") if activation_ref else "default",
                target_resource_ref=target_resource_ref,
                activation_ref=activation_ref,
            )
            node = await self.repository.accept_node_intent(run_id, intent, selected_target=target)
            if node.state == "completed":
                current_record = await self.repository.get_run(run_id)
                previous = next(
                    (
                        attempt.get("result_ref")
                        for attempt in reversed(current_record.attempts)
                        if attempt.get("node_id") == node.node_id and attempt.get("state") == "completed" and attempt.get("result_ref")
                    ),
                    None,
                )
                if previous is None:
                    raise RuntimeError("replay_result_missing")
                task = asyncio.create_task(asyncio.sleep(0, result=ResourceRef.model_validate(previous)))
            else:
                task = asyncio.create_task(self._execute_node(run_id, node, target))
            pending[intent_id] = _PendingNode(intent_id=intent_id, task=task)
            return intent_id

        async def result(handle: str) -> ResourceRef:
            item = pending.get(handle)
            if item is None:
                raise RuntimeError("node_handle_not_found")
            return await item.task

        try:
            try:
                final_ref = await self.executor.run(
                    program,
                    input_binding.input_ref,
                    read_json=read_json,
                    emit_node=emit_node,
                    result=result,
                    materialize_json=materialize_json,
                    register_resource=register_resource,
                    ensure_activation=ensure_activation,
                    wait_activation=wait_activation,
                    defer_cleanup=defer_cleanup,
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                final_ref = await self.executor.run(
                    program,
                    input_binding.input_ref,
                    read_json=read_json,
                    emit_node=emit_node,
                    result=result,
                )
            if pending:
                await asyncio.gather(*(item.task for item in pending.values()))
        except BaseException as exc:
            for item in pending.values():
                if not item.task.done():
                    item.task.cancel()
            if pending:
                await asyncio.gather(*(item.task for item in pending.values()), return_exceptions=True)
            if isinstance(exc, OrchestrationActivationWait):
                await self.repository.activation_wait(
                    run_id,
                    step_id=str(exc.waiting.get("step_id") or ""),
                    package_ref=ResourceRef.model_validate(exc.waiting.get("package_ref")),
                    activation_ref=ResourceRef.model_validate(exc.waiting.get("activation_ref")),
                    waiting=exc.waiting,
                )
                raise
            if not isinstance(exc, asyncio.CancelledError):
                reason = exc.reason if isinstance(exc, OrchestrationProgramError) else {"code": str(exc) or "orchestration_executor_error"}
                if isinstance(exc, OrchestrationExecutorError):
                    reason = {"code": str(exc) or "orchestration_executor_error"}
                current = await self.repository.get_run(run_id)
                if current.state not in {"awaiting_decision", "failed", "cancelled"}:
                    await self.repository.fail_run(run_id, reason)
            raise
        completed = await self.repository.complete_orchestration(run_id, final_ref)
        value = await self.repository._load_json_content(final_ref)
        terminal_state = "completed" if completed.state == "completed" else "decision_required"
        execution_result = ExecutionResult(
            resource_ref=final_ref,
            value=value,
            replay_safety=orchestration_export.replay_safety,
            terminal_state=terminal_state,
            terminal_error=completed.outcome.get("terminal_error") if completed.outcome else None,
        )
        return completed, execution_result

    async def run_cleanup(self, run_id: str) -> Any:
        from loom_v2.observer.cleanup import continuation, registrations

        record = await self.repository.get_run(run_id)
        started = continuation(record)
        if record.state != "cleaning_up" or started is None:
            raise RuntimeError("cleanup_not_running")
        errors = []
        unresolved = [item for item in record.attempts if item.get("node_id") and item.get("state") in {"created", "running", "unknown"}
                      and not any(node.node_id == item["node_id"] and node.intent_id.startswith("cleanup:") for node in record.dynamic_nodes)]
        if unresolved:
            return await self.repository.finish_cleanup(run_id, execution_id=record.execution_id, errors=[{
                "code": "execution_recovery_required", "attempt_ids": [item["attempt_id"] for item in unresolved],
            }])
        deadline = datetime.fromisoformat(started["deadline_at"])
        try:
            async with asyncio.timeout(max(0, (deadline - datetime.now(timezone.utc)).total_seconds())):
                for event in registrations(record):
                    current = await self.repository.get_run(run_id)
                    node = next((item for item in current.dynamic_nodes if item.intent_id == event["intent_id"]), None)
                    if node is not None and node.state == "completed":
                        continue
                    try:
                        if node is not None and node.state not in {"accepted", "dispatched"}:
                            raise RuntimeError("cleanup_result_unconfirmed")
                        package_ref = ResourceRef.model_validate(event["package_ref"])
                        descriptor_ref = ResourceRef.model_validate(event["action_descriptor_ref"])
                        package = await self.repository.get_capability_package(package_ref, run_id=run_id)
                        await self.repository.refresh_slaves()
                        attempt = next((item for item in reversed(current.attempts) if node is not None and item.get("node_id") == node.node_id), None)
                        target = str(attempt["target"]) if attempt else self._select_target(package, descriptor_ref, current)
                        if node is None:
                            node = await self.repository.accept_node_intent(run_id, NodeIntent(
                                intent_id=event["intent_id"], execution_id=record.execution_id,
                                package_ref=package_ref, action_descriptor_ref=descriptor_ref,
                                input_refs=[ResourceRef.model_validate(value) for value in event["input_refs"]],
                            ), selected_target=target)
                        await self._execute_node(run_id, node, target)
                    except Exception as exc:
                        if str(getattr(exc, "reason", exc)) == "stale_driver_epoch":
                            raise
                        errors.append({"step_id": event["step_id"], "resource_ref": event["arguments"]["resource_ref"], "code": str(exc)})
        except TimeoutError:
            errors.append({"code": "cleanup_deadline_exceeded"})
        return await self.repository.finish_cleanup(run_id, execution_id=record.execution_id, errors=errors)

    def _select_target(
        self,
        package: CapabilityPackageVersion,
        action_descriptor_ref: ResourceRef,
        record: Any,
        *,
        exclude_targets: set[str] | None = None,
    ) -> str:
        closure = record.committed.snapshot if getattr(record, "committed", None) is not None else record.draft.snapshot
        allowed_effects = set(record.closure_contract.allowed_effects) if record.closure_contract is not None else set()
        try:
            action_export = package.export_for(action_descriptor_ref)
        except ValueError as exc:
            raise RuntimeError("node_action_export_not_found") from exc
        if action_export.permissions and not set(action_export.permissions).issubset(allowed_effects):
            raise RuntimeError("node_permission_denied")
        available = sorted(
            slave_id
            for slave_id in self.repository.slave_capabilities
            if self.repository._slave_supports_package(slave_id, package)
            and (slave_id in self.workers or slave_id in self.slaves)
            and slave_id not in (exclude_targets or set())
        )
        if not available:
            raise RuntimeError("node_target_unavailable")
        locality = next(
            (
                requirement.value
                for requirement in closure.compute.requirements
                if requirement.key == "loom.data.locality.v1"
            ),
            None,
        )
        if locality is not None:
            requested = str(locality)
            if requested not in available:
                raise RuntimeError("node_target_unavailable")
            return requested
        activation_targets = {
            activation.target_slave
            for activation in record.capability_activations
            if activation.package_version_ref == package.version_ref
            and activation.activation_state == "ready"
        }
        candidates = [slave_id for slave_id in available if slave_id in activation_targets] or available
        cursor_key = (
            f"{package.version_ref}#descriptor:"
            f"{action_descriptor_ref.digest or action_descriptor_ref.resource_id}"
        )
        cursor = self._round_robin_cursor.get(cursor_key, 0)
        target = candidates[cursor % len(candidates)]
        self._round_robin_cursor[cursor_key] = cursor + 1
        return target

    async def _execute_node(self, run_id: str, node: DynamicNode, target: str) -> ResourceRef:
        try:
            current = await self.repository.get_run(run_id)
            active_attempt = next(
                (
                    item
                    for item in reversed(current.attempts)
                    if item.get("node_id") == node.node_id and item.get("state") in {"created", "running"}
                ),
                None,
            )
            if node.state == "accepted":
                attempt = await self.repository.dispatch_dynamic_node(run_id, node.node_id, target=target)
            elif node.state == "dispatched" and active_attempt is not None:
                attempt = active_attempt
                await self.repository.refresh_slaves()
                source = self.repository.slave_instances.get(
                    (str(attempt["target"]), str(attempt["target_instance_id"]))
                )
                if source is not None and source.get("lease_state") in {"expired", "released"}:
                    attempt = await self._recover_attempt(run_id, node, attempt)
            else:
                raise RuntimeError("dynamic_node_not_dispatchable")
            while True:
                try:
                    return await self._execute_attempt(run_id, node, attempt)
                except WorkerUnavailableError:
                    attempt = await self._recover_attempt(run_id, node, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                record = await self.repository.get_run(run_id)
                attempt = next((item for item in reversed(record.attempts) if item.get("node_id") == node.node_id), None)
                await self.repository.fail_dynamic_node(
                    run_id,
                    node.node_id,
                    attempt_id=attempt.get("attempt_id") if attempt else None,
                    error={"code": str(exc) or "dynamic_node_failed"},
                )
            except Exception:
                pass
            raise RuntimeError("dynamic_node_failed") from exc

    async def _recover_attempt(
        self,
        run_id: str,
        node: DynamicNode,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        source_target = str(attempt["target"])
        source_instance_id = str(attempt["target_instance_id"])
        source_worker = self.workers.get(source_target)
        recovery_timeout = float(getattr(source_worker, "action_timeout", 90.0))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + recovery_timeout
        while True:
            await self.repository.refresh_slaves()
            source = self.repository.slave_instances.get((source_target, source_instance_id))
            if source is not None and source.get("lease_state") in {"expired", "released"}:
                break
            if loop.time() >= deadline:
                raise RuntimeError("node_recovery_exhausted")
            await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))

        package = await self.repository.get_capability_package(node.package_ref, run_id=run_id)
        while loop.time() < deadline:
            await self.repository.refresh_slaves()
            record = await self.repository.get_run(run_id)
            try:
                replacement_target = self._select_target(
                    package,
                    node.action_descriptor_ref,
                    record,
                    exclude_targets={source_target},
                )
                return await self.repository.reassign_dynamic_node(
                    run_id,
                    node.node_id,
                    lost_attempt_id=str(attempt["attempt_id"]),
                    expected_execution_id=str(record.execution_id),
                    expected_execution_epoch=int(record.execution_epoch),
                    target=replacement_target,
                    reason="worker_lease_expired",
                )
            except (RuntimeError, ValueError) as exc:
                if str(exc) != "node_target_unavailable":
                    raise
            await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
        raise RuntimeError("node_recovery_exhausted")

    async def _execute_attempt(
        self,
        run_id: str,
        node: DynamicNode,
        attempt: dict[str, Any],
    ) -> ResourceRef:
        package = await self.repository.get_capability_package(node.package_ref, run_id=run_id)
        attempt_id = str(attempt["attempt_id"])
        target = str(attempt["target"])
        record = await self.repository.get_run(run_id)
        try:
            action_export = package.export_for(node.action_descriptor_ref)
        except ValueError as exc:
            raise RuntimeError("node_action_export_not_found") from exc
        descriptor_ref = node.action_descriptor_ref
        action_ref = descriptor_ref.resource_id
        if len(node.input_refs) == 1:
            execution_input_ref = node.input_refs[0]
        else:
            values = [await self.repository._load_json_content(input_ref) for input_ref in node.input_refs]
            execution_input_ref = await self.repository.put_content({"inputs": values}, media_type="application/json")
        binding = ComputeBinding(
            binding_id=f"binding-{node.node_id}",
            hole_id=node.node_id,
            action_descriptor_ref=descriptor_ref,
            capability_package_ref=node.package_ref,
            target_resource_ref=ResourceRef(resource_id=self.repository.names.name("slaves", target)),
            instance_name=node.instance_name,
        )
        closure = TaskClosure(
            closure_id=node.node_id,
            program={"action_ref": action_ref, "io_contract_ref": action_export.io_contract_ref.model_dump(mode="json")},
            node_input_bindings=[
                NodeInputBinding(node_id=action_ref, input_ref=execution_input_ref)
            ],
        )
        idempotency_key = f"{record.execution_id}-{node.node_id}-{package.package_digest}"
        desired = await self.repository.set_capability_desired(
            node.package_ref,
            target,
            desired_state="running",
            idempotency_key=idempotency_key,
            run_id=run_id,
            activation_closure_version_ref=(
                record.committed.version_id
                if record.committed
                else package.package_closure_version_ref
            ),
            compute_binding_ref=binding.binding_id,
            instance_name=node.instance_name,
            target_resource_ref=node.target_resource_ref,
        )
        command = CapabilityProvisionCommand(
            command_id=f"provision-{node.node_id}",
            package_version_ref=package.version_ref,
            package_digest=package.package_digest,
            target_slave=target,
            workspace_id=record.closure_contract.workspace_id if record.closure_contract else "workspace-default",
            activation_closure_version_ref=record.committed.version_id if record.committed else package.package_closure_version_ref,
            compute_binding=binding,
            idempotency_key=desired.last_idempotency_key,
            activation_revision=desired.activation_revision,
            instance_name=node.instance_name,
            target_resource_ref=desired.target_resource_ref,
            instance_config=dict(desired.instance_config),
        )
        action = action_name(action_ref)

        # In production the immutable Worker envelopes are submitted to
        # Observer.  Observer creates the Attempt, forwards both envelopes
        # through the active Slave lease, and records the terminal result.
        # Embedded repositories intentionally keep the local test adapter.
        execute_dynamic_node = getattr(self.repository, "execute_dynamic_node", None)
        if execute_dynamic_node is not None:
            provision_payload: dict[str, Any] = {
                "command": command.model_dump(mode="json"),
                "package": package.model_dump(mode="json"),
            }
            dispatch_payload: dict[str, Any] = {
                "action": action,
                "payload": {},
                "closure": closure.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
                "instance_name": node.instance_name,
            }
            if self.driver_id is not None:
                provision_payload["driver_id"] = self.driver_id
                dispatch_payload["driver_id"] = self.driver_id
            if self.driver_epoch is not None:
                provision_payload["driver_epoch"] = self.driver_epoch
                dispatch_payload["driver_epoch"] = self.driver_epoch
            envelope = await execute_dynamic_node(
                run_id,
                node.node_id,
                target=target,
                provision_payload=provision_payload,
                dispatch_payload=dispatch_payload,
            )
            terminal = envelope.get("terminal_report") or {}
            if str(terminal.get("state") or "") != "completed":
                raise RuntimeError(str(terminal.get("error") or "dynamic_node_failed"))
            stored_attempt = envelope.get("attempt") or {}
            result_ref = stored_attempt.get("result_ref")
            if not isinstance(result_ref, dict):
                raise RuntimeError("dynamic_node_result_missing")
            return ResourceRef.model_validate(result_ref)

        worker = self.workers.get(target)
        if worker is not None:
            report = await worker.provision(command=command, package=package, driver_id=self.driver_id, driver_epoch=self.driver_epoch)
            await self.repository.record_capability_health(report, run_id=run_id)
            result = await worker.dispatch(
                attempt_id=attempt_id,
                execution_id=record.execution_id,
                execution_epoch=record.execution_epoch,
                workspace_id=record.closure_contract.workspace_id if record.closure_contract else "workspace-default",
                action=action,
                payload={},
                closure=closure,
                binding=binding,
                instance_name=node.instance_name,
                driver_id=self.driver_id,
                driver_epoch=self.driver_epoch,
            )
        else:
            slave = self.slaves.get(target)
            if slave is None:
                raise RuntimeError(f"node_target_unavailable:{target}")
            report = await slave.provision(command, package)
            await self.repository.record_capability_health(report, run_id=run_id)
            result = await slave.run(
                attempt_id,
                action,
                {},
                closure=closure,
                binding=binding,
                execution_epoch=record.execution_epoch,
            )
        completed_node = await self.repository.record_dynamic_node_result(
            run_id,
            node.node_id,
            {
                "attempt_id": attempt_id,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "value": result.value,
                "terminal_state": result.terminal_state,
                "terminal_error": result.terminal_error,
                "validation_evidence": result.validation_evidence,
            },
        )
        if completed_node.state != "completed":
            raise RuntimeError("dynamic_node_failed")
        updated = await self.repository.get_run(run_id)
        attempt = next(item for item in updated.attempts if item.get("attempt_id") == attempt_id)
        return ResourceRef.model_validate(attempt["result_ref"])
