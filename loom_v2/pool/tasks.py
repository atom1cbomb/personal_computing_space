from __future__ import annotations
import ast
import asyncio
from datetime import datetime, timezone
from copy import deepcopy
import json
import hmac
import shutil
from typing import Any
from uuid import uuid4
from sqlalchemy import text, select, or_, func
from loom_v2.contracts.constraints import Constraint
from loom_v2.contracts.refinement import is_monotonic_tightening
from loom_v2.contracts.types import ActionExport, CapabilityHealthReport, CapabilityPackageActivation, CapabilityPackageVersion, ExecutionContract, ClosureContract, ClosureVersion, ComputeBinding, ComputeSpec, DynamicNode, IoContract, NodeIntent, ResourceRef, TaskClosure, TypedHole, NodeInputBinding, ValidationEvidence
from loom_v2.db.models import DriverRequestRow, DriverThreadRow, IdempotencyRow, MessageReceiptRow, RunExecutionRequestRow, RunRow, RunEventRow, RuntimeAgentRow
from loom_v2.contracts.agents import DRIVER_COMMANDS, DRIVER_RUN_SCOPED_COMMANDS, DriverCommand, DriverThreadBinding
from loom_v2.contracts.messages import MessageReceipt, MessageReceiptState
from loom_v2.content_store import canonical_json_bytes
from loom_v2.contracts.io_schema import ValidationError as SchemaValidationError, validate, validate_schema
from loom_v2.contracts.package_contracts import DRIVER_ORCHESTRATOR_KEY
from loom_v2.contracts.refs import input_binding_for, action_name, package_body_ref, require_content_ref, single_export
from loom_v2.slave.executor import default_registry
from loom_v2.driver.orchestrator import docker_runtime_available
from loom_v2.contracts.errors import DomainError, DomainErrorEnvelope
from loom_v2.pool.orchestration_preflight import OrchestrationPreflightError, check_program, validate_entry_signature
from loom_v2.repository_state import (ExecutionRequest, PatchReceipt, PlanSummary, RunCapabilities, RunRecord, RunSummary, _transition, execution_transaction, run_record_payload)

class TaskRepository:
    @staticmethod
    def _message_receipt_from_row(row: MessageReceiptRow) -> MessageReceipt:
        values = {
            column: getattr(row, column)
            for column in (
                "workspace_id", "request_id", "conversation_ref", "prompt", "state",
                "run_id", "assistant_text", "outcome", "claim_token", "attempt_count",
                "next_attempt_at", "created_at", "updated_at",
            )
        }
        return MessageReceipt.model_validate(values)


    @staticmethod
    def _message_receipt_values(receipt: MessageReceipt) -> dict[str, Any]:
        return receipt.model_dump(mode="python")


    async def _load_message_receipt(self, workspace_id: str, request_id: str) -> MessageReceipt | None:
        key = (workspace_id, request_id)
        cached = self.message_receipts.get(key)
        if cached is not None:
            return cached
        if self.sessions is None:
            return None
        async with self.sessions() as session:
            row = await session.get(MessageReceiptRow, key)
            if row is None:
                return None
            receipt = self._message_receipt_from_row(row)
            self.message_receipts[key] = receipt
            return receipt


    async def _persist_message_receipt(self, receipt: MessageReceipt) -> None:
        key = (receipt.workspace_id, receipt.request_id)
        previous = self.message_receipts.get(key)
        previous_state = previous.state if previous is not None else None
        if self.sessions is None:
            self.message_receipts[key] = receipt
            if previous_state != receipt.state:
                self._record_message_receipt_event(receipt)
            return
        values = self._message_receipt_values(receipt)
        async with self.sessions() as session:
            row = await session.get(MessageReceiptRow, key)
            if row is None:
                session.add(MessageReceiptRow(**values))
            else:
                if previous_state is None:
                    previous_state = row.state
                for name, value in values.items():
                    setattr(row, name, value)
            await session.commit()
        self.message_receipts[key] = receipt
        if previous_state != receipt.state:
            self._record_message_receipt_event(receipt)


    def _record_message_receipt_event(self, receipt: MessageReceipt) -> None:
        self.message_receipt_events.append(
            {
                "phase": "message_receipt",
                "workspace_id": receipt.workspace_id,
                "conversation_ref": receipt.conversation_ref,
                "request_id": receipt.request_id,
                "state": receipt.state,
                "created_at": receipt.updated_at.isoformat(),
            }
        )


    async def create_or_get_message_receipt(
        self,
        workspace_id: str,
        request_id: str,
        conversation_ref: str,
        prompt: str,
    ) -> MessageReceipt:
        async with self._message_receipt_lock:
            return await self._create_or_get_message_receipt(workspace_id, request_id, conversation_ref, prompt)


    async def _create_or_get_message_receipt(
        self,
        workspace_id: str,
        request_id: str,
        conversation_ref: str,
        prompt: str,
    ) -> MessageReceipt:
        existing = await self._load_message_receipt(workspace_id, request_id)
        if existing is not None:
            if existing.conversation_ref != conversation_ref or existing.prompt != prompt:
                raise ValueError("request_id_reused")
            return existing
        now = datetime.now(timezone.utc)
        receipt = MessageReceipt(
            workspace_id=workspace_id,
            request_id=request_id,
            conversation_ref=conversation_ref,
            prompt=prompt,
            state="accepted",
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        if self.sessions is not None:
            # A concurrent create can race the composite primary key.  The
            # first committed receipt remains authoritative; reload it when
            # the insert loses the race.
            from sqlalchemy.exc import IntegrityError

            try:
                await self._persist_message_receipt(receipt)
            except IntegrityError:
                existing = await self._load_message_receipt(workspace_id, request_id)
                if existing is None:
                    raise
                if existing.conversation_ref != conversation_ref or existing.prompt != prompt:
                    raise ValueError("request_id_reused")
                return existing
        else:
            await self._persist_message_receipt(receipt)
        return receipt


    async def get_message_receipt(self, workspace_id: str, request_id: str) -> MessageReceipt | None:
        return await self._load_message_receipt(workspace_id, request_id)


    async def list_message_receipts(
        self,
        workspace_id: str,
        conversation_ref: str | None = None,
    ) -> list[MessageReceipt]:
        if self.sessions is None:
            receipts = list(self.message_receipts.values())
        else:
            async with self.sessions() as session:
                statement = select(MessageReceiptRow).where(MessageReceiptRow.workspace_id == workspace_id)
                if conversation_ref is not None:
                    statement = statement.where(MessageReceiptRow.conversation_ref == conversation_ref)
                rows = (await session.scalars(statement.order_by(MessageReceiptRow.created_at))).all()
            receipts = [self._message_receipt_from_row(row) for row in rows]
            for receipt in receipts:
                self.message_receipts[(receipt.workspace_id, receipt.request_id)] = receipt
        if conversation_ref is not None:
            receipts = [item for item in receipts if item.workspace_id == workspace_id and item.conversation_ref == conversation_ref]
        else:
            receipts = [item for item in receipts if item.workspace_id == workspace_id]
        return sorted(receipts, key=lambda item: item.created_at)


    async def list_dispatchable_message_receipts(self, workspace_id: str) -> list[MessageReceipt]:
        now = datetime.now(timezone.utc)
        if self.sessions is None:
            receipts = list(self.message_receipts.values())
        else:
            async with self.sessions() as session:
                dispatch_states = ["accepted", "queued", "retryable"]
                rows = (
                    await session.scalars(
                select(MessageReceiptRow)
                        .where(MessageReceiptRow.workspace_id == workspace_id)
                        .where(MessageReceiptRow.state.in_(dispatch_states))
                        .order_by(MessageReceiptRow.created_at)
                    )
                ).all()
            receipts = [self._message_receipt_from_row(row) for row in rows]
            for receipt in receipts:
                self.message_receipts[(receipt.workspace_id, receipt.request_id)] = receipt
        return [
            receipt
            for receipt in receipts
            if receipt.workspace_id == workspace_id
            and (
                (
                    receipt.state in {"accepted", "queued", "retryable"}
                    and (receipt.next_attempt_at is None or self._utc_timestamp(receipt.next_attempt_at) <= now.timestamp())
                )
            )
        ]


    async def queue_message_receipt(self, workspace_id: str, request_id: str) -> MessageReceipt:
        async with self._message_receipt_lock:
            receipt = await self._load_message_receipt(workspace_id, request_id)
            if receipt is None:
                raise KeyError(request_id)
            if receipt.state in {"accepted", "retryable"}:
                receipt = receipt.model_copy(update={"state": "queued", "updated_at": datetime.now(timezone.utc)})
                await self._persist_message_receipt(receipt)
            return receipt


    async def retry_message_receipt(self, workspace_id: str, request_id: str, *, reason: dict[str, Any] | None = None) -> MessageReceipt:
        async with self._message_receipt_lock:
            receipt = await self._load_message_receipt(workspace_id, request_id)
            if receipt is None:
                raise KeyError(request_id)
            if receipt.state in {"completed", "failed", "interrupted"}:
                return receipt
            if receipt.state == "in_flight" and receipt.claim_token:
                return receipt
            now = datetime.now(timezone.utc)
            delay = min(60.0, max(0.25, 0.25 * (2 ** min(receipt.attempt_count, 8))))
            updated = receipt.model_copy(
                update={
                    "state": "retryable",
                    "next_attempt_at": datetime.fromtimestamp(now.timestamp() + delay, tz=timezone.utc),
                    "updated_at": now,
                    "outcome": {"error": deepcopy(reason or {"code": "driver_unavailable"})},
                }
            )
            await self._persist_message_receipt(updated)
            return updated


    async def interrupt_message_receipt(self, workspace_id: str, request_id: str) -> MessageReceipt:
        async with self._message_receipt_lock:
            receipt = await self._load_message_receipt(workspace_id, request_id)
            if receipt is None:
                raise KeyError(request_id)
            if receipt.state in {"completed", "failed", "interrupted"}:
                return receipt
            if receipt.state == "in_flight":
                return receipt
            now = datetime.now(timezone.utc)
            updated = receipt.model_copy(
                update={
                    "state": "interrupted",
                    "claim_token": None,
                    "next_attempt_at": None,
                    "updated_at": now,
                    "outcome": {"error": {"code": "user_interrupt"}},
                }
            )
            await self._persist_message_receipt(updated)
            return updated


    async def claim_message_receipt(
        self,
        workspace_id: str,
        request_id: str,
        *,
        claim_token: str,
        conversation_ref: str | None = None,
    ) -> MessageReceipt:
        async with self._message_receipt_lock:
            return await self._claim_message_receipt(
                workspace_id,
                request_id,
                claim_token=claim_token,
                conversation_ref=conversation_ref,
            )


    async def _claim_message_receipt(
        self,
        workspace_id: str,
        request_id: str,
        *,
        claim_token: str,
        conversation_ref: str | None = None,
    ) -> MessageReceipt:
        receipt = await self._load_message_receipt(workspace_id, request_id)
        if receipt is None:
            raise KeyError(request_id)
        if conversation_ref is not None and receipt.conversation_ref != conversation_ref:
            raise ValueError("request_id_reused")
        if receipt.state in {"completed", "failed", "interrupted"}:
            return receipt
        if receipt.state == "in_flight":
            return receipt
        if receipt.state not in {"accepted", "queued", "retryable", "in_flight"}:
            raise ValueError("invalid_receipt_state")
        now = datetime.now(timezone.utc)
        updated = receipt.model_copy(
            update={
                "state": "in_flight",
                "claim_token": claim_token,
                "attempt_count": receipt.attempt_count + 1,
                "next_attempt_at": None,
                "updated_at": now,
            }
        )
        await self._persist_message_receipt(updated)
        return updated


    async def update_message_receipt(
        self,
        workspace_id: str,
        request_id: str,
        *,
        claim_token: str,
        state: MessageReceiptState | None = None,
        assistant_text: str | None = None,
        run_id: str | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> MessageReceipt:
        async with self._message_receipt_lock:
            return await self._update_message_receipt(
                workspace_id,
                request_id,
                claim_token=claim_token,
                state=state,
                assistant_text=assistant_text,
                run_id=run_id,
                outcome=outcome,
            )


    async def _update_message_receipt(
        self,
        workspace_id: str,
        request_id: str,
        *,
        claim_token: str,
        state: MessageReceiptState | None = None,
        assistant_text: str | None = None,
        run_id: str | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> MessageReceipt:
        receipt = await self._load_message_receipt(workspace_id, request_id)
        if receipt is None:
            raise KeyError(request_id)
        if receipt.state in {"completed", "failed", "interrupted"}:
            if state is None or state == receipt.state:
                return receipt
            raise ValueError("receipt_terminal")
        if receipt.claim_token != claim_token:
            raise ValueError("stale_claim_token")
        new_state = state or receipt.state
        if new_state not in {"in_flight", "completed", "failed", "interrupted", "retryable"}:
            raise ValueError("invalid_receipt_transition")
        now = datetime.now(timezone.utc)
        updates: dict[str, Any] = {"state": new_state, "updated_at": now}
        if assistant_text is not None:
            updates["assistant_text"] = assistant_text
        if run_id is not None:
            updates["run_id"] = run_id
        if outcome is not None:
            updates["outcome"] = deepcopy(outcome)
        if new_state in {"completed", "failed", "interrupted", "retryable"}:
            updates["claim_token"] = None
        if new_state == "retryable":
            updates["next_attempt_at"] = now
        updated = receipt.model_copy(update=updates)
        await self._persist_message_receipt(updated)
        return updated


    async def release_message_receipt(self, workspace_id: str, request_id: str, *, claim_token: str) -> MessageReceipt:
        return await self.update_message_receipt(
            workspace_id,
            request_id,
            claim_token=claim_token,
            state="retryable",
            outcome={"error": {"code": "delivery_released"}},
        )


    async def recover_message_receipts_for_driver(self, workspace_id: str) -> int:
        """Fence claims from a prior Driver epoch in the single-driver deployment."""
        async with self._message_receipt_lock:
            now = datetime.now(timezone.utc)
            if self.sessions is None:
                recovered = 0
                for key, receipt in list(self.message_receipts.items()):
                    if receipt.workspace_id != workspace_id or receipt.state != "in_flight":
                        continue
                    self.message_receipts[key] = receipt.model_copy(
                        update={
                            "state": "retryable",
                            "claim_token": None,
                            "next_attempt_at": now,
                            "updated_at": now,
                        }
                    )
                    recovered += 1
                return recovered
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(MessageReceiptRow)
                        .where(
                            MessageReceiptRow.workspace_id == workspace_id,
                            MessageReceiptRow.state == "in_flight",
                        )
                        .with_for_update()
                    )
                ).all()
                for row in rows:
                    row.state = "retryable"
                    row.claim_token = None
                    row.next_attempt_at = now
                    row.updated_at = now
                await session.commit()
                for row in rows:
                    receipt = self._message_receipt_from_row(row)
                    self.message_receipts[(receipt.workspace_id, receipt.request_id)] = receipt
                return len(rows)


    async def bind_thread(self, binding: DriverThreadBinding) -> dict[str, Any]:
        existing_thread = next(
            (
                current
                for key, current in self.driver_threads.items()
                if key[0] == binding.workspace_id
                and current.thread_id == binding.thread_id
                and key != (binding.workspace_id, binding.conversation_ref)
            ),
            None,
        )
        if existing_thread is not None:
            raise ValueError("thread_binding_conflict")
        if self.sessions is not None:
            async with self.sessions() as session:
                conflict = (
                    await session.scalars(
                        select(DriverThreadRow).where(
                            DriverThreadRow.workspace_id == binding.workspace_id,
                            DriverThreadRow.thread_id == binding.thread_id,
                            DriverThreadRow.conversation_ref != binding.conversation_ref,
                        )
                    )
                ).first()
                if conflict is not None:
                    raise ValueError("thread_binding_conflict")
                row = await session.get(DriverThreadRow, (binding.workspace_id, binding.conversation_ref))
                values = binding.model_dump(mode="json")
                values["updated_at"] = datetime.now(timezone.utc)
                if row is None:
                    session.add(DriverThreadRow(**values))
                else:
                    for key, value in values.items():
                        if key not in {"workspace_id", "conversation_ref"}:
                            setattr(row, key, value)
                await session.commit()
        self.driver_threads[(binding.workspace_id, binding.conversation_ref)] = binding
        return binding.model_dump(mode="json")


    async def thread_bind(self, binding: DriverThreadBinding | dict[str, Any]) -> dict[str, Any]:
        return await self.bind_thread(binding if isinstance(binding, DriverThreadBinding) else DriverThreadBinding.model_validate(binding))


    async def get_thread(self, workspace_id: str, conversation_ref: str) -> dict[str, Any] | None:
        key = (workspace_id, conversation_ref)
        if key in self.driver_threads:
            return self.driver_threads[key].model_dump(mode="json")
        if self.sessions is None:
            return None
        async with self.sessions() as session:
            row = await session.get(DriverThreadRow, key)
        if row is None:
            return None
        binding = DriverThreadBinding.model_validate({column: getattr(row, column) for column in ("workspace_id", "conversation_ref", "thread_id", "model", "workspace_root", "last_turn_id", "turn_state", "active_request_id", "driver_epoch")})
        self.driver_threads[key] = binding
        return binding.model_dump(mode="json")


    async def thread_get(self, workspace_id: str, conversation_ref: str) -> dict[str, Any] | None:
        return await self.get_thread(workspace_id, conversation_ref)


    async def set_turn_state(self, workspace_id: str, conversation_ref: str, *, turn_state: str, request_id: str | None = None, turn_id: str | None = None, driver_epoch: int | None = None) -> dict[str, Any]:
        current = await self.get_thread(workspace_id, conversation_ref)
        if current is None:
            raise KeyError(conversation_ref)
        current.update({"turn_state": turn_state})
        if request_id is not None:
            current["active_request_id"] = request_id
        if turn_id is not None:
            current["last_turn_id"] = turn_id
        if driver_epoch is not None:
            current["driver_epoch"] = driver_epoch
        return await self.bind_thread(DriverThreadBinding.model_validate(current))


    async def turn_state(self, workspace_id: str, conversation_ref: str, turn_state: str, **kwargs: Any) -> dict[str, Any]:
        return await self.set_turn_state(workspace_id, conversation_ref, turn_state=turn_state, **kwargs)


    async def _assert_run_workspace(self, run_id: str, workspace_id: str) -> RunRecord:
        record = await self._load(run_id)
        contract_workspace = record.closure_contract.workspace_id if record.closure_contract is not None else "workspace-default"
        if contract_workspace != workspace_id:
            raise ValueError("workspace_binding_mismatch")
        return record


    async def execute_driver_command(
        self,
        command: DriverCommand,
    ) -> dict[str, Any]:
        request_key = (command.driver_id, command.request_id)
        lock = self._driver_request_locks.setdefault(request_key, asyncio.Lock())
        async with lock:
            async with self._execution_transaction(command.arguments.get("run_id")):
                session = self._execution_session.get()
                if session is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": f"driver-request:{command.driver_id}:{command.request_id}"})
                return await self._execute_driver_command(command)


    async def _admit_driver_arguments(self, name: str, args: dict[str, Any], workspace_id: str) -> None:
        """Check resource scope and normalize package refs before dispatch."""
        if name in DRIVER_RUN_SCOPED_COMMANDS and args.get("run_id") is not None:
            try:
                await self._assert_run_workspace(str(args["run_id"]), workspace_id)
            except KeyError:
                if name != "node.fail":
                    raise
        if name in {"capability.get", "capability.desire"}:
            package_ref = args.get("package_ref")
            if isinstance(package_ref, dict):
                try:
                    package_ref = ResourceRef.model_validate(package_ref)
                except (TypeError, ValueError) as exc:
                    if name == "capability.get":
                        raise
                    raise ValueError("capability_package_ref_invalid") from exc
            args["package_ref"] = package_ref
            await self._assert_package_workspace(package_ref, workspace_id, run_id=args.get("run_id"))
        elif name == "capability.health" and args.get("run_id") is None:
            report = args.get("report") or {}
            await self._assert_package_workspace(str(report.get("package_version_ref") or ""), workspace_id)


    async def _execute_driver_command(
        self,
        command: DriverCommand,
    ) -> dict[str, Any]:
        if command.command not in DRIVER_COMMANDS:
            raise ValueError("driver_command_not_allowed")
        requested_workspace = command.arguments.get("workspace_id")
        agents = await self.list_agents(str(requested_workspace or "workspace-default"), role="driver")
        if requested_workspace is None and not agents:
            if self.sessions is None:
                all_workspaces = {key[0] for key in self.agents if key[1] == "driver"}
            else:
                async with self.sessions() as session:
                    all_workspaces = set(
                        (await session.scalars(select(RuntimeAgentRow.workspace_id).where(RuntimeAgentRow.role == "driver"))).all()
                    )
            for workspace in all_workspaces:
                agents.extend(await self.list_agents(workspace, role="driver"))
        agent = next((item for item in agents if item["agent_id"] == command.driver_id and item["lease_state"] == "active"), None)
        if agent is None or agent["instance_id"] != command.instance_id or int(agent["epoch"]) != int(command.driver_epoch):
            raise ValueError("stale_driver_epoch")
        record = await self._agent_record(agent["workspace_id"], "driver", command.driver_id, command.instance_id)
        if record is None or not hmac.compare_digest(str(record.get("lease_id_hash", "")), self._lease_hash(command.lease_id)):
            raise ValueError("stale_driver_epoch")
        request_key = (command.driver_id, command.request_id)
        if self.sessions is None and request_key in self._driver_requests:
            receipt = self._driver_requests[request_key]
            if receipt.get("command") != command.command:
                raise ValueError("request_id_reused")
            return deepcopy(receipt["response"])
        if self.sessions is not None:
            async with self.sessions() as session:
                existing = await session.get(DriverRequestRow, request_key)
                if existing is not None:
                    if existing.command != command.command:
                        raise ValueError("request_id_reused")
                    return deepcopy(existing.response)
        args = dict(command.arguments)
        workspace_id = str(agent["workspace_id"])
        await self._admit_driver_arguments(command.command, args, workspace_id)
        run_id = command.arguments.get("run_id")
        if run_id and command.command not in {"run.get", "execution.get", "run.readiness"}:
            claims = await self.list_execution_requests(agent["workspace_id"], run_id=run_id)
            if claims and not any(i.state == "claimed" and (i.driver_id, i.driver_instance_id, i.driver_epoch) ==
                (command.driver_id, command.instance_id, command.driver_epoch) for i in claims):
                raise ValueError("stale_execution_owner")
        if command.command == "run.open":
            contract = args.get("closure_contract")
            result = (await self.open_run(args.get("run_id"), str(args.get("task_ref") or args.get("conversation_ref") or ""), str(args.get("goal") or (contract or {}).get("goal") or ""), bool(args.get("allow_reassignment", False)), ClosureContract.model_validate(contract) if contract else None, user_id=str(args.get("user_id", "user-default")), workspace_id=workspace_id)).__dict__
            result = {"run_id": result["run_id"], "state": result["state"], "draft_version": result["draft"].version_id, "draft_digest": result["draft"].snapshot_digest}
        elif command.command == "run.begin":
            result = run_record_payload(await self.begin_refinement(str(args["run_id"])))
        elif command.command == "run.get":
            result = run_record_payload(await self.get_run(str(args["run_id"])))
        elif command.command == "run.patch":
            run = await self.get_run(str(args["run_id"]))
            receipt = await self.apply_patch(run.run_id, run.draft_version, run.draft_digest, str(args.get("operation_id") or command.request_id), list(args.get("ops", [])))
            result = {"receipt": receipt.receipt, "run_id": receipt.run_id, "draft_version": receipt.draft_version, "draft_digest": receipt.draft_digest, "patch_cursor": receipt.patch_cursor, "readiness": receipt.readiness}
        elif command.command == "run.commit":
            result_version = await self.commit(str(args["run_id"]), str(args["version_id"]), str(args["digest"]))
            result = {"closure_version": result_version.version_id, "snapshot_digest": result_version.snapshot_digest}
        elif command.command == "execution.enqueue":
            item = await self.enqueue_committed_run(
                workspace_id=workspace_id,
                request_id=str(args["request_id"]),
                run_id=str(args["run_id"]),
                closure_version=str(args["closure_version"]),
                conversation_ref=args.get("conversation_ref"),
            )
            result = item.model_dump()
        elif command.command == "execution.claim_next":
            result = await self.claim_next_execution(
                workspace_id=workspace_id,
                driver_id=command.driver_id,
                driver_instance_id=command.instance_id,
                driver_epoch=command.driver_epoch,
            ) or {}
        elif command.command == "execution.get":
            result = (await self.get_execution_request(workspace_id, str(args["request_id"]))).model_dump()
        elif command.command == "execution.recover":
            result = {
                "recovered": await self.recover_execution_requests_for_driver(
                    workspace_id,
                    driver_id=command.driver_id,
                    driver_instance_id=command.instance_id,
                    driver_epoch=command.driver_epoch,
                )
            }
        elif command.command == "run.result":
            reported = dict(args["result"])
            final_ref = reported.pop("orchestration_final_ref", None)
            if final_ref is not None:
                result = run_record_payload(await self.complete_orchestration(str(args["run_id"]), ResourceRef.model_validate(final_ref)))
            else:
                result = run_record_payload(await self.record_result(str(args["run_id"]), reported))
        elif command.command == "run.close":
            result = run_record_payload(await self.close_run(str(args["run_id"])))
        elif command.command == "run.cancel":
            reason = args.get("reason", "user_interrupt")
            if not isinstance(reason, (str, dict)):
                reason = str(reason)
            result = run_record_payload(await self.cancel_run(str(args["run_id"]), reason))
        elif command.command == "run.fail":
            reason = args.get("reason", {"code": "driver_failure"})
            if not isinstance(reason, (str, dict)):
                reason = str(reason)
            result = run_record_payload(await self.fail_run(str(args["run_id"]), reason))
        elif command.command == "run.resolve":
            result = run_record_payload(await self.resolve_run(str(args["run_id"]), str(args.get("decision") or "")))
        elif command.command == "run.readiness":
            result = await self.inspect_readiness(str(args["run_id"]), args.get("version_id"))
        elif command.command == "run.recovery.list":
            result = {
                "runs": [
                    run_record_payload(item)
                    for item in await self._records_with_states({"thinking", "running"})
                    if item.state in {"thinking", "running"}
                    and (item.closure_contract is None or item.closure_contract.workspace_id == workspace_id)
                ]
            }
        elif command.command == "run.recovery.mark":
            active_run_ids = {str(run_id) for run_id in args.get("active_run_ids", [])}
            for run_id in active_run_ids:
                await self._assert_run_workspace(run_id, workspace_id)
            result = {"recovered": await self.recover_stale_runs(active_run_ids)}
        elif command.command == "message.append":
            result = await self.append_message(str(args["run_id"]), str(args["role"]), str(args["content"]), request_id=args.get("request_id") or command.request_id)
        elif command.command == "message.claim":
            receipt = await self.claim_message_receipt(
                workspace_id,
                str(args["request_id"]),
                claim_token=str(args["claim_token"]),
                conversation_ref=args.get("conversation_ref"),
            )
            result = receipt.model_dump(mode="json")
        elif command.command == "message.update":
            receipt = await self.update_message_receipt(
                workspace_id,
                str(args["request_id"]),
                claim_token=str(args["claim_token"]),
                state=args.get("state"),
                assistant_text=args.get("assistant_text"),
                run_id=args.get("run_id"),
                outcome=args.get("outcome"),
            )
            result = receipt.model_dump(mode="json")
        elif command.command == "message.release":
            receipt = await self.release_message_receipt(
                workspace_id,
                str(args["request_id"]),
                claim_token=str(args["claim_token"]),
            )
            result = receipt.model_dump(mode="json")
        elif command.command == "agent_signal.record":
            result = await self.record_agent_signal(str(args["run_id"]), str(args["kind"]), dict(args.get("payload", {})))
        elif command.command == "thread.bind":
            payload = dict(args)
            payload["workspace_id"] = workspace_id
            payload["driver_epoch"] = command.driver_epoch
            result = await self.bind_thread(DriverThreadBinding.model_validate(payload))
        elif command.command == "thread.get":
            result = await self.get_thread(workspace_id, str(args["conversation_ref"])) or {}
        elif command.command == "turn.state":
            result = await self.set_turn_state(workspace_id, str(args["conversation_ref"]), turn_state=str(args["turn_state"]), request_id=args.get("request_id"), turn_id=args.get("turn_id"), driver_epoch=command.driver_epoch)
        elif command.command == "capability.list":
            if args.get("run_id") is not None:
                await self._assert_run_workspace(str(args["run_id"]), workspace_id)
            await self.refresh_slaves(workspace_id)
            slave_agents = list(self.slave_agents.values())
            capabilities = [
                {
                    "resource_id": self.names.name("slaves", agent["agent_id"]),
                    "available": agent.get("lease_state") == "active",
                    "actions": sorted(self.slave_capabilities.get(str(agent["agent_id"]), {}).get("actions", set())),
                    "executor_descriptors": (agent.get("capabilities") or {}).get("executor_descriptors", []),
                    "runtime_plugin_descriptors": (agent.get("capabilities") or {}).get("runtime_plugin_descriptors", (agent.get("capabilities") or {}).get("runtime_plugins", [])),
                    "term_support": (agent.get("capabilities") or {}).get("term_support", []),
                }
                for agent in slave_agents
            ]
            result = {
                "capabilities": capabilities,
                "packages": [item.model_dump(mode="json") for item in await self.list_capability_packages(run_id=args.get("run_id"), include_abandoned=bool(args.get("include_abandoned", False)), workspace_id=workspace_id)],
            }
        elif command.command == "capability.get":
            result = (await self.get_capability_package(args["package_ref"], run_id=args.get("run_id"))).model_dump(mode="json")
        elif command.command == "capability.activations":
            package = await self._assert_package_workspace(ResourceRef.model_validate(args["package_ref"]), workspace_id)
            result = {"activations": [item.model_dump(mode="json") for item in await self.list_package_activations(package)]}
        elif command.command == "capability.desire":
            activation = await self.set_capability_desired(
                args["package_ref"],
                str(args["target_slave"]),
                desired_state=str(args["desired_state"]),
                idempotency_key=str(args["idempotency_key"]),
                run_id=args.get("run_id"),
                workspace_id=workspace_id,
                activation_closure_version_ref=str(args.get("activation_closure_version_ref") or ""),
                compute_binding_ref=str(args.get("compute_binding_ref") or ""),
                rebuild=bool(args.get("rebuild", False)),
                instance_name=str(args.get("instance_name") or "default"),
                target_resource_ref=ResourceRef.model_validate(args["target_resource_ref"]) if args.get("target_resource_ref") else None,
                instance_config=dict(args["instance_config"]) if args.get("instance_config") is not None else None,
                credential_profile=args.get("credential_profile"),
            )
            result = activation.model_dump(mode="json")
        elif command.command == "capability.health":
            result = (await self.record_capability_health(CapabilityHealthReport.model_validate(args["report"]), run_id=args.get("run_id"))).model_dump(mode="json")
        elif command.command == "node.accept":
            node = await self.accept_node_intent(str(args["run_id"]), NodeIntent.model_validate(args["intent"]), selected_target=str(args["selected_target"]))
            result = node.model_dump(mode="json")
        elif command.command == "node.dispatch":
            attempt = await self.dispatch_dynamic_node(str(args["run_id"]), str(args["node_id"]), target=str(args["target"]))
            result = {"attempt": attempt}
        elif command.command == "resource.register":
            resource_payload = args.get("resource")
            if resource_payload is None:
                resource_payload = {
                    key: value for key, value in args.items() if key not in {"workspace_id", "run_id"}
                }
            if not isinstance(resource_payload, dict):
                raise ValueError("resource_payload_invalid")
            result = (await self.register_resource(workspace_id, resource_payload)).model_dump(mode="json")
        elif command.command in {"cleanup.register", "resource.register_program", "content.materialize", "activation.ensure_program"}:
            from loom_v2.pool import cleanup

            methods = {
                "cleanup.register": cleanup.register_cleanup,
                "resource.register_program": cleanup.register_program_resource,
                "content.materialize": cleanup.materialize_json,
                "activation.ensure_program": cleanup.ensure_program_activation,
            }
            payload = {key: value for key, value in args.items() if key not in {"workspace_id", "run_id"}}
            for key in ("resource_ref", "cleanup_action_ref", "package_ref", "managed_by_ref", "target_resource_ref"):
                if payload.get(key) is not None:
                    payload[key] = ResourceRef.model_validate(payload[key])
            async with self._execution_transaction():
                value = await methods[command.command](self, str(args["run_id"]), **payload)
            result = value.model_dump(mode="json") if isinstance(value, ResourceRef) else value
        elif command.command == "cleanup.finish":
            from loom_v2.pool.cleanup import finish_cleanup

            async with self._execution_transaction():
                items = await self.list_execution_requests(workspace_id, states={"claimed"})
                if not any(item.run_id == args["run_id"] and item.execution_id == args["execution_id"]
                           and item.driver_id == command.driver_id and item.driver_instance_id == command.instance_id
                           and item.driver_epoch == command.driver_epoch for item in items):
                    raise ValueError("stale_execution_owner")
                result = run_record_payload(await finish_cleanup(
                    self, str(args["run_id"]), execution_id=str(args["execution_id"]), errors=list(args.get("errors") or []),
                ))
        elif command.command == "activation.wait":
            result = run_record_payload(await self.activation_wait(
                str(args["run_id"]),
                step_id=str(args.get("step_id") or ""),
                package_ref=ResourceRef.model_validate(args["package_ref"]),
                activation_ref=ResourceRef.model_validate(args["activation_ref"]),
                waiting=dict(args.get("waiting") or {}),
            ))
        elif command.command == "node.reassign":
            attempt = await self.reassign_dynamic_node(
                str(args["run_id"]),
                str(args["node_id"]),
                lost_attempt_id=str(args["lost_attempt_id"]),
                expected_execution_id=str(args["expected_execution_id"]),
                expected_execution_epoch=int(args["expected_execution_epoch"]),
                target=str(args["target"]),
                reason=str(args.get("reason") or "worker_lease_expired"),
            )
            result = {
                "node": {"node_id": str(args["node_id"]), "state": "dispatched"},
                "attempt": attempt,
            }
        elif command.command == "node.result":
            node = await self.record_dynamic_node_result(str(args["run_id"]), str(args["node_id"]), dict(args["result"]))
            result = node.model_dump(mode="json")
        elif command.command == "node.fail":
            node = await self.fail_dynamic_node(
                str(args["run_id"]),
                str(args["node_id"]),
                attempt_id=args.get("attempt_id"),
                error=dict(args.get("reason") or {}),
            )
            result = node.model_dump(mode="json")
        else:
            raise ValueError(f"unsupported_driver_command:{command.command}")
        self._driver_requests[request_key] = {"command": command.command, "response": deepcopy(result)}
        if self.sessions is not None:
            async with self._execution_write_session() as session:
                session.add(DriverRequestRow(driver_id=command.driver_id, request_id=command.request_id, command=command.command, response=deepcopy(result)))
        return result


    async def recover_persisted_activation_waits(self) -> list[str]:
        await self._load_workspace_packages()
        awakened: list[str] = []
        for record in await self._records_with_states({"awaiting_auth"}):
            if record.state != "awaiting_auth":
                continue
            waiting = next(
                (
                    event
                    for event in reversed(record.events)
                    if event.get("phase") == "activation_wait"
                ),
                None,
            )
            if not waiting:
                continue
            activation_payload = waiting.get("activation_ref")
            if not isinstance(activation_payload, dict):
                continue
            try:
                activation_ref = ResourceRef.model_validate(activation_payload)
            except (TypeError, ValueError):
                continue
            binding = activation_ref.access_binding
            package_version_ref = str(binding.get("package_version_ref") or "")
            target_slave = str(binding.get("target_slave") or "")
            instance_name = str(binding.get("instance_name") or "default")
            revision = binding.get("activation_revision")
            if not package_version_ref or not target_slave or revision is None:
                continue
            try:
                revision = int(revision)
            except (TypeError, ValueError):
                continue
            candidates = list(record.capability_activations)
            candidates.extend(
                activation
                for activations in self.workspace_package_activations.values()
                for activation in activations
            )
            activation = next(
                (
                    item
                    for item in candidates
                    if item.package_version_ref == package_version_ref
                    and item.package_digest == activation_ref.digest
                    and item.target_slave == target_slave
                    and item.instance_name == instance_name
                    and item.activation_revision == revision
                    and item.activation_state == "ready"
                ),
                None,
            )
            if activation is None:
                continue
            awakened.extend(await self.recover_activation_wait(activation))
        return awakened


    async def recover_stale_runs(self, active_run_ids: set[str] | None = None) -> list[str]:
        """Fence Runs left active by a lost Observer/Driver process.

        ``thinking`` and ``running`` are leased states: they require a live
        Driver owner.  A process restart cannot reconstruct that in-memory
        owner, so persisted records in those states must not remain visible as
        active forever.  The optional owner set lets a caller preserve Runs
        that it has explicitly reattached before running recovery; the normal
        Observer startup path passes no owners because its Driver starts empty.
        """

        owners = active_run_ids or set()
        recovered: list[str] = []
        for record in await self._records_with_states({"thinking", "running"}):
            if record.run_id in owners or record.state not in {"thinking", "running"}:
                continue
            if record.state == "running" and record.execution_id is not None:
                snapshot = record.committed.snapshot if record.committed is not None else record.draft.snapshot
                if (
                    snapshot.program_systems.execution.kind,
                    snapshot.program_systems.execution.version,
                ) == DRIVER_ORCHESTRATOR_KEY[1:]:
                    package = (
                        self._find_package(snapshot.program_systems.package_ref, run_id=record.run_id)
                        if snapshot.program_systems.package_ref
                        else None
                    )
                    replayable = (
                        package is not None
                        and single_export(package).replay_safety == "DeterministicByEventLog"
                        and not bool(package.body.get("captures_run_state", False))
                        and not package.body.get("captured_secret_refs")
                        and not package.body.get("captured_path_refs")
                    )
                    if replayable:
                        reason = {"code": "driver_restarted", "action": "replay_orchestration"}
                        record.execution_epoch += 1
                        for attempt in record.attempts:
                            if attempt.get("state") in {"created", "running"}:
                                attempt["state"] = "failed"
                                attempt["terminal_error"] = reason
                        for node in record.dynamic_nodes:
                            if node.state == "dispatched":
                                node.state = "accepted"
                        record.events.append(
                            {
                                "phase": "run_recovered",
                                "run_id": record.run_id,
                                "execution_id": record.execution_id,
                                "reason": reason,
                                "execution_epoch": record.execution_epoch,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                        await self._persist(record)
                        recovered.append(record.run_id)
                        continue
            reason = {
                "code": "orchestration_not_replayable"
                if record.state == "running"
                and record.committed is not None
                and (
                    record.committed.snapshot.program_systems.execution.kind,
                    record.committed.snapshot.program_systems.execution.version,
                )
                == DRIVER_ORCHESTRATOR_KEY[1:]
                else "observer_restarted"
            }
            for attempt in record.attempts:
                if attempt.get("state") in {"created", "running"}:
                    attempt["state"] = "failed"
                    attempt["terminal_error"] = reason
            _transition(record, "run_failed", reason=reason)
            record.outcome = {
                "disposition": "failed",
                "decision": None,
                "terminal_state": "failed",
                "terminal_error": reason,
                "reason": reason,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
            }
            record.events.append(
                {
                    "phase": "run_recovered",
                    "run_id": record.run_id,
                    "execution_id": record.execution_id,
                    "reason": reason,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._persist(record)
            recovered.append(record.run_id)
        return recovered


    @staticmethod
    def _record_from_row(row: RunRow, events: list) -> RunRecord:
        draft_payload = TaskRepository._normalize_version_payload(row.draft)
        committed_payload = TaskRepository._normalize_version_payload(row.committed) if row.committed else None
        contract_payload = TaskRepository._normalize_contract_payload(row.closure_contract) if row.closure_contract else None
        raw_dynamic_nodes = getattr(row, "dynamic_nodes", None) or []
        if raw_dynamic_nodes:
            dynamic_nodes = [DynamicNode.model_validate(item) for item in raw_dynamic_nodes]
        else:
            dynamic_nodes = TaskRepository._dynamic_nodes_from_events(events)
        return RunRecord(
            run_id=row.run_id,
            task_ref=row.task_ref,
            goal=row.goal,
            draft=ClosureVersion.model_validate(draft_payload),
            closure_contract=ClosureContract.model_validate(contract_payload) if contract_payload else None,
            allow_reassignment=row.allow_reassignment,
            committed=ClosureVersion.model_validate(committed_payload) if committed_payload else None,
            execution_id=row.execution_id,
            execution_epoch=row.execution_epoch,
            state=row.state,
            outcome=row.outcome,
            attempts=row.attempts or [],
            events=events,
            capability_packages=[CapabilityPackageVersion.model_validate(item) for item in (getattr(row, "capability_packages", None) or [])],
            capability_activations=[CapabilityPackageActivation.model_validate(item) for item in (getattr(row, "capability_activations", None) or [])],
            dynamic_nodes=dynamic_nodes,
        )


    @staticmethod
    def _dynamic_nodes_from_events(events: list[dict[str, Any]]) -> list[DynamicNode]:
        nodes: dict[str, DynamicNode] = {}
        for event in events:
            phase = event.get("phase")
            node_id = event.get("node_id")
            if phase == "node_accepted" and node_id:
                try:
                    package_ref = ResourceRef.model_validate(event["package_ref"])
                    action_descriptor_ref = ResourceRef.model_validate(
                        event["action_descriptor_ref"]
                    )
                    input_refs = [ResourceRef.model_validate(item) for item in event.get("input_refs", [])]
                    nodes[node_id] = DynamicNode(
                        node_id=node_id,
                        parent_execution_ref=str(event.get("execution_id") or ""),
                        intent_id=str(event.get("intent_id") or ""),
                        package_ref=package_ref,
                        action_descriptor_ref=action_descriptor_ref,
                        input_refs=input_refs,
                        instance_name=str(event.get("instance_name") or "default"),
                        state="accepted",
                    )
                except (TypeError, ValueError):
                    continue
            elif node_id and phase in {"node_reassigned", "node_dispatched"} and node_id in nodes:
                nodes[node_id].state = "dispatched"
            elif node_id and phase == "node_completed" and node_id in nodes:
                state = str(event.get("terminal_state") or "completed")
                if state in {"completed", "failed", "decision_required"}:
                    nodes[node_id].state = state
        return list(nodes.values())


    @staticmethod
    def _normalize_task_closure_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(payload)
        for section in ("program", "compute"):
            values = normalized.get(section)
            if not isinstance(values, dict):
                continue
            action_ref = values.get("action_ref")
            if isinstance(action_ref, dict):
                values["action_ref"] = (
                    action_ref.get("program_ref")
                    or action_ref.get("action_ref")
                    or action_ref.get("ref")
                    or ""
                )
        return normalized


    @classmethod
    def _normalize_version_payload(cls, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        normalized = deepcopy(payload)
        snapshot = normalized.get("snapshot")
        if isinstance(snapshot, dict):
            normalized["snapshot"] = cls._normalize_task_closure_payload(snapshot)
        return normalized


    @classmethod
    def _normalize_contract_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(payload)
        body = normalized.get("body")
        if isinstance(body, dict):
            normalized["body"] = cls._normalize_task_closure_payload(body)
        return normalized


    async def _persist(self, record: RunRecord) -> None:
        self._run_capabilities[record.run_id] = RunCapabilities(record.run_id,
            record.closure_contract.workspace_id if record.closure_contract else None,
            record.capability_packages, record.capability_activations)
        loaded = self._execution_loaded_runs.get()
        if loaded is not None:
            loaded.add(record.run_id)
        if self.sessions is None:
            return
        async with self._execution_write_session() as session:
            row = await session.get(RunRow, record.run_id)
            values = {
                "run_id": record.run_id,
                "task_ref": record.task_ref,
                "goal": record.goal,
                "closure_contract": record.closure_contract.model_dump(mode="json") if record.closure_contract else None,
                "allow_reassignment": record.allow_reassignment,
                "draft": record.draft.model_dump(mode="json"),
                "committed": record.committed.model_dump(mode="json") if record.committed else None,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "state": record.state,
                "latest_event_at": self._record_order_key(record),
                "outcome": record.outcome,
                "attempts": record.attempts,
                "capability_packages": [item.model_dump(mode="json") for item in record.capability_packages],
                "capability_activations": [item.model_dump(mode="json") for item in record.capability_activations],
                "dynamic_nodes": [item.model_dump(mode="json") for item in record.dynamic_nodes],
            }
            last_sequence = (await session.scalar(select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == record.run_id))) or 0
            if last_sequence > len(record.events):
                raise ValueError("run_history_truncated")
            session.add_all([RunEventRow(run_id=record.run_id, sequence=i + 1, event=event)
                for i, event in enumerate(record.events) if i >= last_sequence])
            if row is None:
                row = RunRow(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)


    async def _load(self, run_id: str) -> RunRecord:
        loaded = self._execution_loaded_runs.get()
        if loaded is not None and run_id not in loaded:
            session = self._execution_session.get()
            if session is not None:
                row = await session.get(RunRow, run_id, with_for_update=True, populate_existing=True)
                if row is None:
                    raise KeyError(run_id)
                self.runs[run_id] = await self._read_record(session, row)
            loaded.add(run_id)
        if run_id in self.runs:
            return self.runs[run_id]
        if self.sessions is None:
            raise KeyError(run_id)
        async with self.sessions() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            record = await self._read_record(session, row)
            self.runs[run_id] = record
            return record


    async def _records_with_states(self, states: set[str]) -> list[RunRecord]:
        """Select current work before loading any execution history."""
        if self.sessions is None:
            return [record for record in self.runs.values() if record.state in states]
        query = select(RunRow.run_id).where(RunRow.state.in_(states)).order_by(RunRow.run_id)
        session = self._execution_session.get()
        if session is not None:
            run_ids = (await session.scalars(query)).all()
        else:
            async with self.sessions() as session:
                run_ids = (await session.scalars(query)).all()
        return [await self._load(run_id) for run_id in run_ids]


    async def read_run_result(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        if self.sessions is None:
            record = self.runs.get(run_id)
            if record is None or record.closure_contract is None or record.closure_contract.workspace_id != workspace_id:
                raise KeyError(run_id)
            return {
                "state": record.state, "outcome": deepcopy(record.outcome), "run_id": record.run_id,
                "closure_version": record.committed.version_id if record.committed else None,
                "snapshot_digest": record.committed.snapshot_digest if record.committed else None,
                "execution_id": record.execution_id, "execution_epoch": record.execution_epoch,
            }
        async with self.sessions() as session:
            row = (await session.execute(select(
                RunRow.state, RunRow.outcome, RunRow.run_id,
                RunRow.committed["version_id"].as_string().label("closure_version"),
                RunRow.committed["snapshot_digest"].as_string().label("snapshot_digest"),
                RunRow.execution_id, RunRow.execution_epoch,
            ).where(
                RunRow.run_id == run_id,
                RunRow.closure_contract["workspace_id"].as_string() == workspace_id,
            ))).one_or_none()
        if row is None:
            raise KeyError(run_id)
        return dict(row._mapping)


    async def read_frozen_plan(self, workspace_id: str, run_id: str, closure_version: str) -> dict[str, Any]:
        """Read the immutable contract saved when this version entered the queue."""
        if self.sessions is None:
            for item in self.execution_requests.values():
                if (item.workspace_id == workspace_id and item.run_id == run_id and item.closure_version == closure_version
                        and item.snapshot_digest):
                    return deepcopy(item.closure_contract)
            raise KeyError((run_id, closure_version))
        query = select(RunExecutionRequestRow.closure_contract).where(
            RunExecutionRequestRow.workspace_id == workspace_id,
            RunExecutionRequestRow.run_id == run_id,
            RunExecutionRequestRow.closure_version == closure_version,
            RunExecutionRequestRow.snapshot_digest != "",
        ).order_by(RunExecutionRequestRow.created_at).limit(1)
        async with self.sessions() as session:
            body = (await session.execute(query)).scalar_one_or_none()
        if body is None:
            raise KeyError((run_id, closure_version))
        return body


    @staticmethod
    def _execution_request_from_row(row: RunExecutionRequestRow) -> ExecutionRequest:
        created = row.created_at
        updated = row.updated_at
        if not isinstance(created, datetime):
            created = datetime.now(timezone.utc)
        if not isinstance(updated, datetime):
            updated = created
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return ExecutionRequest(
            workspace_id=row.workspace_id,
            request_id=row.request_id,
            request_payload=deepcopy(row.request_payload),
            run_id=row.run_id,
            closure_version=row.closure_version,
            closure_contract=deepcopy(row.closure_contract),
            snapshot_digest=row.snapshot_digest,
            source=row.source,
            conversation_ref=row.conversation_ref,
            state=row.state,
            driver_id=row.driver_id,
            driver_instance_id=row.driver_instance_id,
            driver_epoch=row.driver_epoch,
            execution_id=row.execution_id,
            execution_epoch=row.execution_epoch,
            outcome=deepcopy(row.outcome),
            started_at=row.started_at,
            deadline_at=row.deadline_at,
            created_at=created,
            updated_at=updated,
        )


    async def _load_execution_request(self, workspace_id: str, request_id: str) -> ExecutionRequest | None:
        key = (workspace_id, request_id)
        loaded = self._execution_loaded_requests.get()
        if loaded is not None and key not in loaded:
            session = self._execution_session.get()
            if session is not None:
                row = await session.get(RunExecutionRequestRow, key, with_for_update=True, populate_existing=True)
                if row is None:
                    self.execution_requests.pop(key, None)
                else:
                    self.execution_requests[key] = self._execution_request_from_row(row)
            loaded.add(key)
            return self.execution_requests.get(key)
        cached = self.execution_requests.get(key)
        if cached is not None:
            return cached
        if self.sessions is None:
            return None
        async with self.sessions() as session:
            row = await session.get(RunExecutionRequestRow, key)
        if row is None:
            return None
        item = self._execution_request_from_row(row)
        self.execution_requests[key] = item
        return item


    async def _persist_execution_request(self, item: ExecutionRequest) -> None:
        loaded = self._execution_loaded_requests.get()
        if loaded is not None:
            loaded.add((item.workspace_id, item.request_id))
        item.updated_at = datetime.now(timezone.utc)
        self.execution_requests[(item.workspace_id, item.request_id)] = item
        if self.sessions is None:
            return
        values = {
            "workspace_id": item.workspace_id,
            "request_id": item.request_id,
            "request_payload": deepcopy(item.request_payload),
            "run_id": item.run_id,
            "closure_version": item.closure_version,
            "source": item.source,
            "closure_contract": deepcopy(item.closure_contract),
            "snapshot_digest": item.snapshot_digest,
            "conversation_ref": item.conversation_ref,
            "state": item.state,
            "driver_id": item.driver_id,
            "driver_instance_id": item.driver_instance_id,
            "driver_epoch": item.driver_epoch,
            "execution_id": item.execution_id,
            "execution_epoch": item.execution_epoch,
            "outcome": deepcopy(item.outcome),
            "started_at": item.started_at,
            "deadline_at": item.deadline_at,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        async with self._execution_write_session() as session:
            row = await session.get(RunExecutionRequestRow, (item.workspace_id, item.request_id))
            if row is None:
                session.add(RunExecutionRequestRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)


    async def list_execution_requests(self, workspace_id: str, *, states: set[str] | None = None, run_id: str | None = None, conversation_ref: str | None = None, run_ids: set[str] | None = None) -> list[ExecutionRequest]:
        if self.sessions is not None:
            statement = select(RunExecutionRequestRow).where(RunExecutionRequestRow.workspace_id == workspace_id).order_by(RunExecutionRequestRow.created_at)
            if states is not None:
                statement = statement.where(RunExecutionRequestRow.state.in_(states))
            if run_id is not None:
                statement = statement.where(RunExecutionRequestRow.run_id == run_id)
            if conversation_ref is not None:
                statement = statement.where(or_(RunExecutionRequestRow.conversation_ref == conversation_ref,
                                                RunExecutionRequestRow.run_id.in_(run_ids or set())))
            session = self._execution_session.get()
            if session is not None:
                rows = (await session.scalars(statement)).all()
                return [await self._load_execution_request(row.workspace_id, row.request_id) for row in rows]
            async with self.sessions() as session:
                rows = (await session.scalars(statement)).all()
            items = [self._execution_request_from_row(row) for row in rows]
            for item in items:
                self.execution_requests[(item.workspace_id, item.request_id)] = item
            return items
        items = [item for item in self.execution_requests.values() if item.workspace_id == workspace_id]
        if states is not None:
            items = [item for item in items if item.state in states]
        if run_id is not None:
            items = [item for item in items if item.run_id == run_id]
        if conversation_ref is not None:
            items = [item for item in items if item.conversation_ref == conversation_ref or item.run_id in (run_ids or set())]
        if self._execution_in_transaction.get():
            items = [await self._load_execution_request(item.workspace_id, item.request_id) for item in items]
        return sorted(items, key=lambda item: item.created_at)


    async def submit_run_plan(
        self,
        *,
        workspace_id: str,
        request_id: str,
        plan: ClosureContract,
        conversation_ref: str | None = None,
        source: str = "external",
    ) -> ExecutionRequest:
        """Validate, commit and enqueue one complete plan idempotently."""
        payload = {
            "conversation_ref": conversation_ref,
            "source": source,
            "plan": plan.model_dump(mode="json"),
        }
        async with self._execution_transaction(f"submission:{workspace_id}:{request_id}"):
            existing = await self._load_execution_request(workspace_id, request_id)
            if existing is not None:
                if canonical_json_bytes(existing.request_payload) != canonical_json_bytes(payload):
                    raise ValueError("request_id_reused")
                return existing
            if plan.workspace_id not in {workspace_id, ""}:
                raise ValueError("workspace_binding_mismatch")
            task_ref = conversation_ref or ""
            normalized = plan.model_copy(update={
                "workspace_id": workspace_id,
                "origin_conversation_ref": conversation_ref or "",
            }, deep=True)
            record = await self.open_run(
                None,
                task_ref,
                normalized.goal,
                bool(normalized.recovery_policy.get("allow_reassignment", False)),
                normalized,
                user_id=normalized.user_id,
                workspace_id=workspace_id,
            )
            await self.begin_refinement(record.run_id)
            committed = await self.commit(record.run_id, record.draft_version, record.draft_digest)
            item = ExecutionRequest(
                workspace_id=workspace_id,
                request_id=request_id,
                request_payload=payload,
                run_id=record.run_id,
                closure_version=committed.version_id,
                closure_contract=record.closure_contract.model_copy(update={"body": committed.snapshot}).model_dump(mode="json"),
                snapshot_digest=committed.snapshot_digest,
                source=source,
                conversation_ref=conversation_ref,
            )
            await self._persist_execution_request(item)
            record.events.append({
                "phase": "execution_submission",
                "request_id": request_id,
                "source": source,
                "execution_request_state": item.state,
                "conversation_ref": conversation_ref,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            await self._persist(record)
            return item


    async def get_execution_request(self, workspace_id: str, request_id: str) -> ExecutionRequest:
        item = await self._load_execution_request(workspace_id, request_id)
        if item is None:
            raise KeyError(request_id)
        return item


    async def claim_next_execution(
        self,
        *,
        workspace_id: str,
        driver_id: str,
        driver_instance_id: str,
        driver_epoch: int,
    ) -> dict[str, Any] | None:
        """Claim one queued plan and create its execution attempt."""
        async with self._execution_transaction():
            from loom_v2.pool.cleanup import registrations

            if self.sessions is not None:
                session = self._execution_session.get()
                row = (await session.scalars(select(RunExecutionRequestRow).join(RunRow, RunRow.run_id == RunExecutionRequestRow.run_id).where(
                    RunExecutionRequestRow.workspace_id == workspace_id,
                    RunExecutionRequestRow.state.in_({"queued", "recovering"}),
                    RunRow.state.in_({"committed", "running", "cleaning_up", "cancelled", "failed", "completed", "closed"}),
                ).order_by(RunExecutionRequestRow.created_at).limit(1).with_for_update(skip_locked=True, of=RunRow))).first()
                items = [await self._load_execution_request(workspace_id, row.request_id)] if row else []
            else:
                items = await self.list_execution_requests(workspace_id, states={"queued", "recovering", "awaiting_auth"})
            for item in items:
                record = await self._load(item.run_id)
                if item.state == "awaiting_auth":
                    if item.deadline_at is None or item.deadline_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
                        continue
                    await self.fail_run(record.run_id, {"code": "execution_deadline_exceeded"})
                    record = await self._load(item.run_id)
                finished = any(event.get("phase") == "cleanup_finished" and event.get("execution_id") == record.execution_id for event in record.events)
                if record.state in {"cancelled", "failed", "completed", "awaiting_decision"} and registrations(record) and not finished:
                    await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
                if record.state == "cleaning_up" and item.state == "recovering":
                    await self._persist_execution_claim(record, item, driver_id, driver_instance_id, driver_epoch)
                    return {"request": item.model_dump(), "run": run_record_payload(record)}
                if item.state == "settled":
                    continue
                if record.state in {"cancelled", "failed", "completed", "closed"}:
                    item.state = "settled"
                    item.outcome = deepcopy(record.outcome)
                    await self._persist_execution_request(item)
                    continue
                if record.state == "running" and item.state == "recovering" and item.execution_id == record.execution_id:
                    await self._persist_execution_claim(record, item, driver_id, driver_instance_id, driver_epoch)
                    return {"request": item.model_dump(), "run": run_record_payload(record)}
                if record.state != "committed" or item.closure_version != (record.committed.version_id if record.committed else None):
                    continue
                readiness = await self._evaluate_readiness(record.committed.snapshot, run_id=record.run_id) if record.committed else {"ready": False, "blockers": [{"code": "closure_not_committed"}]}
                if not readiness.get("ready"):
                    reason = {"code": "readiness_blocked", "blockers": deepcopy(readiness.get("blockers", []))}
                    _transition(record, "run_needs_decision", reason=reason)
                    record.outcome = {
                        "disposition": "awaiting_decision",
                        "decision": "repair",
                        "terminal_state": "failed",
                        "terminal_error": reason,
                        "execution_id": None,
                        "execution_epoch": record.execution_epoch,
                    }
                    await self._persist(record)
                    item.state = "settled"
                    item.outcome = deepcopy(record.outcome)
                    await self._persist_execution_request(item)
                    return {"request": item.model_dump(), "run": run_record_payload(record)}
                started = await self.start(record.run_id, item.closure_version)
                record = await self._load(record.run_id)
                item.execution_id = str(started["execution_id"])
                item.execution_epoch = int(started["execution_epoch"])
                item.started_at = datetime.now(timezone.utc)
                timeout_value = (record.closure_contract.resource_budget if record.closure_contract else {}).get(
                    "execution_timeout_seconds",
                    self.settings.execution_timeout_seconds,
                )
                try:
                    timeout_seconds = max(1.0, float(timeout_value))
                except (TypeError, ValueError):
                    timeout_seconds = self.settings.execution_timeout_seconds
                item.deadline_at = datetime.fromtimestamp(item.started_at.timestamp() + timeout_seconds, tz=timezone.utc)
                if record.attempts:
                    record.attempts[-1].update({
                        "driver_id": driver_id,
                        "driver_instance_id": driver_instance_id,
                        "driver_epoch": driver_epoch,
                    })
                    await self._persist(record)
                await self._persist_execution_claim(record, item, driver_id, driver_instance_id, driver_epoch)
                return {"request": item.model_dump(), "run": run_record_payload(record)}
            return None


    async def _persist_execution_claim(self, record, item, driver_id, driver_instance_id, driver_epoch):
        """Persist ownership and its audit event in the same Run transaction."""
        recovered = item.state == "recovering"
        item.state = "claimed"
        item.driver_id = driver_id
        item.driver_instance_id = driver_instance_id
        item.driver_epoch = driver_epoch
        await self._persist_execution_request(item)
        record.events.append({
            "phase": "execution_claimed",
            "request_id": item.request_id,
            "driver_id": driver_id,
            "driver_instance_id": driver_instance_id,
            "driver_epoch": driver_epoch,
            **({"recovered": True} if recovered else {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        await self._persist(record)


    async def recover_execution_requests_for_driver(
        self,
        workspace_id: str,
        *,
        driver_id: str,
        driver_instance_id: str,
        driver_epoch: int,
    ) -> int:
        """Recover expired owners, rechecking each claim under its Run lock."""
        active = {(a["agent_id"], a["instance_id"], a["epoch"])
            for a in await self.list_agents(workspace_id, "driver") if a["lease_state"] == "active"}
        if self.sessions is None:
            candidates = [(i.run_id, i.request_id, i.driver_id, i.driver_instance_id, i.driver_epoch)
                for i in await self.list_execution_requests(workspace_id, states={"claimed"})]
        else:
            async with self.sessions() as session:
                candidates = (await session.execute(select(RunExecutionRequestRow.run_id, RunExecutionRequestRow.request_id,
                    RunExecutionRequestRow.driver_id, RunExecutionRequestRow.driver_instance_id, RunExecutionRequestRow.driver_epoch)
                    .where(RunExecutionRequestRow.workspace_id == workspace_id, RunExecutionRequestRow.state == "claimed")
                    .order_by(RunExecutionRequestRow.run_id))).all()
        recovered = 0
        for run_id, request_id, owner_id, owner_instance, owner_epoch in candidates:
            if (owner_id, owner_instance, owner_epoch) in active:
                continue
            async with self._execution_transaction(run_id):
                # Match mutation/claim lock order without decoding Run history.
                session = self._execution_session.get()
                if session is not None:
                    await session.scalar(select(RunRow.run_id).where(RunRow.run_id == run_id).with_for_update())
                item = await self._load_execution_request(workspace_id, request_id)
                if item is None or item.state != "claimed":
                    continue
                active = {(a["agent_id"], a["instance_id"], a["epoch"])
                    for a in await self.list_agents(workspace_id, "driver") if a["lease_state"] == "active"}
                if (item.driver_id, item.driver_instance_id, item.driver_epoch) in active:
                    continue
                item.state = "recovering"
                item.driver_id = None
                item.driver_instance_id = None
                item.driver_epoch = None
                await self._persist_execution_request(item)
                recovered += 1
        return recovered


    @execution_transaction
    async def materialize_program_json(self, run_id: str, **arguments: Any) -> ResourceRef:
        from loom_v2.pool.cleanup import materialize_json

        return await materialize_json(self, run_id, **arguments)


    @execution_transaction
    async def register_program_resource(self, run_id: str, **arguments: Any) -> ResourceRef:
        from loom_v2.pool.cleanup import register_program_resource

        return await register_program_resource(self, run_id, **arguments)


    @execution_transaction
    async def register_cleanup(self, run_id: str, **arguments: Any) -> dict[str, Any]:
        from loom_v2.pool.cleanup import register_cleanup

        return await register_cleanup(self, run_id, **arguments)


    @execution_transaction
    async def ensure_program_activation(self, run_id: str, **arguments: Any) -> ResourceRef:
        from loom_v2.pool.cleanup import ensure_program_activation

        return await ensure_program_activation(self, run_id, **arguments)


    @execution_transaction
    async def finish_cleanup(self, run_id: str, **arguments: Any) -> RunRecord:
        from loom_v2.pool.cleanup import finish_cleanup

        return await finish_cleanup(self, run_id, **arguments)


    async def activation_wait(
        self,
        run_id: str,
        *,
        step_id: str,
        package_ref: ResourceRef,
        activation_ref: ResourceRef,
        waiting: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Pause one claimed execution without creating another Run."""
        async with self._execution_transaction():
            record = await self._load(run_id)
            if record.state == "awaiting_auth":
                return record
            if record.state != "running":
                raise ValueError("execution_not_running")
            _transition(record, "activation_wait", reason={"step_id": step_id})
            record.events.append({
                "phase": "activation_wait",
                "step_id": step_id,
                "package_ref": package_ref.model_dump(mode="json"),
                "activation_ref": activation_ref.model_dump(mode="json"),
                "waiting": deepcopy(waiting or {}),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            record.outcome = None
            await self._persist(record)
            workspace = record.closure_contract.workspace_id if record.closure_contract else self.settings.workspace_id
            for item in await self.list_execution_requests(workspace, run_id=run_id):
                if item.run_id != run_id or item.state == "settled":
                    continue
                item.state = "awaiting_auth"
                item.driver_id = None
                item.driver_instance_id = None
                item.driver_epoch = None
                item.outcome = None
                await self._persist_execution_request(item)
            package = await self.get_capability_package(package_ref, run_id=run_id)
            for activation in await self.list_package_activations(package):
                await self.recover_activation_wait(activation)
            return record


    async def recover_activation_wait(self, activation: CapabilityPackageActivation) -> list[str]:
        """Wake runs waiting on the exact activation revision."""
        if activation.activation_state != "ready":
            return []
        awakened: list[str] = []
        async with self._execution_transaction():
            for record in await self._records_with_states({"awaiting_auth"}):
                if record.state != "awaiting_auth":
                    continue
                event = next((item for item in reversed(record.events) if item.get("phase") == "activation_wait"), {})
                matching = (
                    event.get("phase") == "activation_wait"
                    and event.get("activation_ref", {}).get("access_binding", {}).get("package_version_ref") == activation.package_version_ref
                    and event.get("activation_ref", {}).get("access_binding", {}).get("target_slave") == activation.target_slave
                    and event.get("activation_ref", {}).get("access_binding", {}).get("instance_name") == activation.instance_name
                    and event.get("activation_ref", {}).get("version_or_digest") == activation.package_digest
                    and event.get("activation_ref", {}).get("access_binding", {}).get("activation_revision") == activation.activation_revision
                )
                if not matching:
                    continue
                _transition(record, "activation_ready")
                record.events.append({"phase": "activation_wait_recovered", "activation_revision": activation.activation_revision, "created_at": datetime.now(timezone.utc).isoformat()})
                await self._persist(record)
                workspace = record.closure_contract.workspace_id if record.closure_contract else self.settings.workspace_id
                for item in await self.list_execution_requests(workspace, run_id=record.run_id, states={"awaiting_auth"}):
                    if item.run_id == record.run_id and item.state == "awaiting_auth":
                        item.state = "recovering"
                        await self._persist_execution_request(item)
                awakened.append(record.run_id)
        return awakened


    @execution_transaction
    async def _settle_execution_request(self, run_id: str, *, execution_id: str | None = None, outcome: dict[str, Any] | None = None) -> None:
        # Resolve the workspace from the Run rather than the process default.
        # This keeps settlement correct when a repository serves more than one
        # workspace and also lets claim-time failures (which have no
        # execution_id yet) settle their queue item.
        try:
            record = await self._load(run_id)
        except KeyError:
            return
        workspace_id = record.closure_contract.workspace_id if record.closure_contract is not None else self.settings.workspace_id
        items = await self.list_execution_requests(workspace_id, run_id=run_id)
        from loom_v2.pool.cleanup import registrations, continuation

        finished = any(event.get("phase") == "cleanup_finished" and event.get("execution_id") == record.execution_id for event in record.events)
        if record.state == "cleaning_up" or (execution_id is not None and execution_id != record.execution_id):
            return
        if registrations(record) and not finished:
            started = continuation(record)
            if started is None:
                timeout = float(record.closure_contract.resource_budget.get("cleanup_timeout_seconds", 60))
                deadline = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + max(1, min(timeout, 300)), tz=timezone.utc)
                started = {
                    "phase": "cleanup_started", "execution_id": record.execution_id,
                    "terminal_state": record.state, "outcome": deepcopy(outcome),
                    "deadline_at": deadline.isoformat(),
                }
                record.events.append(started)
            record.state = "cleaning_up"
            record.outcome = {"cleanup": {"state": "pending"}, "execution_id": record.execution_id}
            matching = [item for item in items if item.run_id == run_id and item.execution_id == record.execution_id]
            if not matching:
                matching = [ExecutionRequest(
                    workspace_id=workspace_id, request_id=f"cleanup:{record.execution_id}", request_payload={},
                    run_id=run_id, closure_version=record.committed.version_id,
                    closure_contract=record.closure_contract.model_copy(update={"body": record.committed.snapshot}).model_dump(mode="json"),
                    snapshot_digest=record.committed.snapshot_digest,
                    execution_id=record.execution_id, execution_epoch=record.execution_epoch,
                )]
            for item in matching:
                item.state = "recovering"
                item.driver_id = item.driver_instance_id = item.driver_epoch = None
                item.deadline_at = datetime.fromisoformat(started["deadline_at"])
                item.outcome = deepcopy(record.outcome)
                await self._persist_execution_request(item)
            await self._persist(record)
            return
        for item in items:
            if item.run_id != run_id or item.state == "settled":
                continue
            if execution_id is not None and item.execution_id not in {None, execution_id}:
                continue
            item.state = "settled"
            item.outcome = deepcopy(outcome)
            await self._persist_execution_request(item)
            record.events.append({
                "phase": "execution_settled",
                "request_id": item.request_id,
                "execution_request_state": item.state,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            await self._persist(record)


    async def enqueue_committed_run(
        self,
        *,
        workspace_id: str,
        request_id: str,
        run_id: str,
        closure_version: str,
        source: str = "builtin",
        conversation_ref: str | None = None,
    ) -> ExecutionRequest:
        """Create an execution queue item for an already committed Run.

        Built-in Driver planning has already performed the open/refine/commit
        sequence.  It uses this method to enter the same durable queue as an
        external submission without creating a second Run.
        """
        async with self._execution_transaction():
            existing = await self._load_execution_request(workspace_id, request_id)
            record = await self._assert_run_workspace(run_id, workspace_id)
            if record.committed is None or record.committed.version_id != closure_version:
                raise ValueError("closure_not_committed")
            payload = {
                "run_id": run_id,
                "closure_version": closure_version,
                "source": source,
                "conversation_ref": conversation_ref,
            }
            if existing is not None:
                if canonical_json_bytes(existing.request_payload) != canonical_json_bytes(payload):
                    raise ValueError("request_id_reused")
                return existing
            # A committed Run may have been enqueued by a retried turn with a
            # different command id.  Reuse the active item instead of creating
            # a second schedulable request.
            active = [
                item for item in await self.list_execution_requests(workspace_id, states={"queued", "claimed", "recovering"}, run_id=run_id)
                if item.run_id == run_id
            ]
            if active:
                return active[0]
            item = ExecutionRequest(
                workspace_id=workspace_id,
                request_id=request_id,
                request_payload=payload,
                run_id=run_id,
                closure_version=closure_version,
                closure_contract=record.closure_contract.model_copy(update={"body": record.committed.snapshot}).model_dump(mode="json"),
                snapshot_digest=record.committed.snapshot_digest,
                source=source,
                conversation_ref=conversation_ref,
            )
            await self._persist_execution_request(item)
            return item


    async def resubmit_run_plan(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request_id: str,
        base_closure_version: str,
        plan: ClosureContract,
    ) -> ExecutionRequest:
        """Replace a repairable plan and enqueue a new execution request."""
        payload = {
            "run_id": run_id,
            "base_closure_version": base_closure_version,
            "plan": plan.model_dump(mode="json"),
        }
        async with self._execution_transaction():
            existing = await self._load_execution_request(workspace_id, request_id)
            if existing is not None:
                if canonical_json_bytes(existing.request_payload) != canonical_json_bytes(payload):
                    raise ValueError("request_id_reused")
                return existing
            record = await self._assert_run_workspace(run_id, workspace_id)
            if record.state != "awaiting_decision" or not isinstance(record.outcome, dict) or record.outcome.get("decision") != "repair":
                raise ValueError("resubmit_not_repairable")
            if record.committed is None or record.committed.version_id != base_closure_version:
                raise ValueError("version_conflict")
            previous_requests = [item for item in await self.list_execution_requests(workspace_id, run_id=run_id) if item.run_id == run_id]
            if not previous_requests or previous_requests[0].source != "external":
                raise ValueError("planning_source_mismatch")
            if record.closure_contract is None or plan.goal != record.goal:
                raise ValueError("closure_goal_mismatch")
            # Repair may change the executable body and resource selection, but
            # it cannot silently weaken the accepted success contract.
            old_contract = record.closure_contract.model_dump(mode="json")
            new_contract = plan.model_dump(mode="json")
            for field_name in ("required_success_criteria", "result_expectations"):
                if canonical_json_bytes(new_contract[field_name]) != canonical_json_bytes(old_contract[field_name]):
                    raise ValueError("success_contract_changed")
            if not set(new_contract["allowed_effects"]).issubset(set(old_contract["allowed_effects"])):
                raise ValueError("success_contract_changed")
            old_constraints = {canonical_json_bytes(item) for item in old_contract["declared_constraints"]}
            new_constraints = {canonical_json_bytes(item) for item in new_contract["declared_constraints"]}
            if not old_constraints.issubset(new_constraints):
                raise ValueError("success_contract_changed")
            if plan.workspace_id not in {workspace_id, ""}:
                raise ValueError("workspace_binding_mismatch")
            old_body_constraints = {canonical_json_bytes(item.model_dump(mode="json")) for item in record.committed.snapshot.constraints}
            new_body_constraints = {canonical_json_bytes(item.model_dump(mode="json")) for item in plan.body.constraints}
            if not old_body_constraints.issubset(new_body_constraints):
                raise ValueError("constraints_changed")
            for key, old_value in record.closure_contract.resource_budget.items():
                new_value = plan.resource_budget.get(key, old_value)
                if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)) and new_value > old_value:
                    raise ValueError("execution_policy_changed")
            if plan.recovery_policy != record.closure_contract.recovery_policy:
                raise ValueError("execution_policy_changed")
            normalized = plan.model_copy(update={
                "workspace_id": workspace_id,
                "origin_conversation_ref": record.closure_contract.origin_conversation_ref,
                "user_id": record.closure_contract.user_id,
            }, deep=True)
            readiness = await self._evaluate_readiness(normalized.body, run_id=run_id)
            if not readiness.get("ready"):
                raise self._readiness_error(readiness)
            previous_version = record.committed
            previous_outcome = deepcopy(record.outcome)
            _transition(record, "refinement_started", reason={"base_closure_version": base_closure_version})
            record.closure_contract = normalized
            record.draft = ClosureVersion(
                version_id=f"draft-{uuid4().hex[:12]}",
                closure_id=normalized.closure_id,
                snapshot=normalized.body.model_copy(deep=True),
                snapshot_digest=normalized.body.canonical_digest(),
                patch_cursor=previous_version.patch_cursor + 1,
            )
            record.committed = None
            record.outcome = None
            record.events.append({
                "phase": "plan_resubmitted",
                "request_id": request_id,
                "base_closure_version": base_closure_version,
                "previous_committed": previous_version.model_dump(mode="json"),
                "previous_outcome": previous_outcome,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            await self._persist(record)
            committed = await self.commit(record.run_id, record.draft.version_id, record.draft.snapshot_digest)
            item = ExecutionRequest(
                workspace_id=workspace_id,
                request_id=request_id,
                request_payload=payload,
                run_id=run_id,
                closure_version=committed.version_id,
                closure_contract=record.closure_contract.model_copy(update={"body": committed.snapshot}).model_dump(mode="json"),
                snapshot_digest=committed.snapshot_digest,
                source="external",
                conversation_ref=normalized.origin_conversation_ref or None,
            )
            await self._persist_execution_request(item)
            return item


    @staticmethod
    def _check_attempt_budget(record: RunRecord) -> None:
        budget = record.closure_contract.resource_budget if record.closure_contract is not None else {}
        raw_max_attempts = budget.get("max_attempts")
        if raw_max_attempts is None:
            return
        try:
            max_attempts = int(raw_max_attempts)
        except (TypeError, ValueError) as exc:
            raise ValueError("orchestration_attempt_limit_exceeded") from exc
        if max_attempts < 0 or len(record.attempts) >= max_attempts:
            raise ValueError("orchestration_attempt_limit_exceeded")


    @staticmethod
    def _schema_blocker(
        *,
        code: str,
        schema_ref: ResourceRef,
        input_ref: ResourceRef | None = None,
        errors: list[SchemaValidationError] | None = None,
    ) -> dict[str, Any]:
        blocker: dict[str, Any] = {
            "code": code,
            "schema_digest": schema_ref.digest,
            "errors": [item.model_dump(mode="json") for item in (errors or [])],
        }
        if input_ref is not None:
            blocker["input_ref"] = input_ref.resource_id
        return blocker


    async def _evaluate_input_schema(self, snapshot: TaskClosure, action_ref: str) -> list[dict[str, Any]]:
        contract_ref = snapshot.program.io_contract_ref
        if contract_ref is None:
            return []
        blockers: list[dict[str, Any]] = []
        try:
            contract_payload = await self._load_json_content(contract_ref)
            io_contract = IoContract.model_validate(contract_payload)
        except (FileNotFoundError, ValueError, TypeError):
            blockers.append({"code": "io_contract_unavailable", "io_contract_ref": contract_ref.resource_id})
            return blockers
        schema_ref = io_contract.input_schema_ref
        if schema_ref is None:
            return blockers
        binding = input_binding_for(snapshot, action_ref)
        if binding is None:
            blockers.append(
                {
                    "code": "payload_missing",
                    "node_id": action_ref or "default",
                    "schema_digest": schema_ref.digest,
                }
            )
            return blockers
        try:
            schema = await self._load_json_content(schema_ref)
            validate_schema(schema)
            value = await self._load_json_content(binding.input_ref)
        except (FileNotFoundError, ValueError, TypeError) as exc:
            error = SchemaValidationError(
                path="$",
                keyword="json",
                message="input content is not valid JSON",
                expected="JSON value",
                observed=str(exc),
            )
            blockers.append(self._schema_blocker(code="payload_schema_mismatch", schema_ref=schema_ref, input_ref=binding.input_ref, errors=[error]))
            return blockers
        errors = validate(schema, value)
        if errors:
            blockers.append(self._schema_blocker(code="payload_schema_mismatch", schema_ref=schema_ref, input_ref=binding.input_ref, errors=errors))
        return blockers


    async def _evaluate_orchestration_package(self, snapshot: TaskClosure, *, run_id: str | None = None) -> list[dict[str, Any]]:
        package_ref = snapshot.program_systems.package_ref
        if package_ref is None:
            return [{"code": "orchestration_package_not_bound"}]
        package = self._find_package(package_ref, run_id=run_id)
        if package is None:
            foreign = self._find_package(package_ref)
            if foreign is not None:
                return [{"code": "capability_package_scope_mismatch", "package_ref": self._package_ref(foreign)}]
            return [{"code": "orchestration_package_not_found"}]
        if (
            package.package_type,
            package.execution.kind,
            package.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY:
            return [{"code": "orchestration_package_invalid"}]
        blockers: list[dict[str, Any]] = []
        try:
            action_export = single_export(package)
            program_content_ref = package_body_ref(package, "program_content_ref")
            allowed_node_package_refs = [
                ResourceRef.model_validate(item)
                for item in package.body["allowed_node_package_refs"]
            ]
            max_nodes = int(package.body["max_nodes"])
            max_live_nodes = int(package.body["max_live_nodes"])
        except (KeyError, TypeError, ValueError):
            return [{"code": "orchestration_package_invalid"}]
        if (
            action_export.replay_safety != "DeterministicByEventLog"
            or bool(package.body.get("captures_run_state", False))
            or package.body.get("captured_secret_refs")
            or package.body.get("captured_path_refs")
        ):
            blockers.append({"code": "orchestration_not_replayable", "package_ref": self._package_ref(package)})
        if max_live_nodes > max_nodes:
            blockers.append({"code": "orchestration_live_node_limit_exceeded", "package_ref": self._package_ref(package), "limit": max_live_nodes, "max_nodes": max_nodes})
        docker_bin = shutil.which("docker")
        if self.orchestrator_runtime_available is not False and not docker_runtime_available(docker_bin):
            blockers.append({"code": "orchestrator_runtime_unavailable"})
        if run_id is not None and not self._package_visible_to_run(package, run_id):
            blockers.append({"code": "capability_package_scope_mismatch", "package_ref": self._package_ref(package)})
        if package.publication_state == "abandoned":
            blockers.append({"code": "capability_package_abandoned", "package_ref": self._package_ref(package)})
        run_record = self.runs.get(run_id) if run_id is not None else None
        budget = run_record.closure_contract.resource_budget if run_record is not None and run_record.closure_contract is not None else {}
        for field_name, budget_key, code in (
            ("max_nodes", "max_nodes", "orchestration_node_limit_exceeded"),
            ("max_live_nodes", "max_live_nodes", "orchestration_live_node_limit_exceeded"),
        ):
            package_limit = max_nodes if field_name == "max_nodes" else max_live_nodes
            budget_limit = budget.get(budget_key)
            try:
                exceeds_budget = budget_limit is not None and package_limit is not None and int(package_limit) > int(budget_limit)
            except (TypeError, ValueError):
                exceeds_budget = True
            if exceeds_budget:
                blockers.append({"code": code, "package_ref": self._package_ref(package), "limit": package_limit, "budget": budget_limit})
        closure_contract_ref = snapshot.program.io_contract_ref
        export_io_contract_ref = action_export.io_contract_ref
        if (
            closure_contract_ref is None
            or export_io_contract_ref is None
            or (closure_contract_ref.digest or closure_contract_ref.resource_id)
            != (export_io_contract_ref.digest or export_io_contract_ref.resource_id)
        ):
            blockers.append({"code": "io_contract_mismatch", "package_ref": self._package_ref(package)})
        stat = await self.content_store.stat(program_content_ref)
        if stat is None or not stat.integrity_verified or stat.declared_digest != program_content_ref.digest:
            blockers.append({"code": "package_content_unavailable", "package_ref": self._package_ref(package)})
        elif stat.media_type != "text/x-python":
            blockers.append({"code": "orchestration_program_invalid", "package_ref": self._package_ref(package)})
        else:
            try:
                source = (await self.content_store.get(program_content_ref)).decode("utf-8")
                ast.parse(source)
            except (UnicodeDecodeError, SyntaxError):
                blockers.append({"code": "orchestration_program_invalid", "package_ref": self._package_ref(package)})
            else:
                signature_blockers = validate_entry_signature(source)
                if signature_blockers:
                    blockers.append(
                        {
                            "code": "orchestration_program_type_error",
                            "diagnostics": [
                                {
                                    "file": "orchestration.py",
                                    "line": 1,
                                    "column": 1,
                                    "end_line": 1,
                                    "end_column": 1,
                                    "severity": "error",
                                    "rule": str(item.get("code", "invalid_entry_annotation")),
                                    "message": str(item.get("message", "invalid orchestration entry signature")),
                                }
                                for item in signature_blockers
                            ],
                        }
                    )
                else:
                    try:
                        diagnostics = await check_program(source)
                    except OrchestrationPreflightError as exc:
                        blockers.append({"code": exc.code, **exc.details})
                    else:
                        unresolved = [item for item in diagnostics if item.get("rule") == "reportUndefinedVariable"]
                        type_errors = [item for item in diagnostics if item.get("rule") != "reportUndefinedVariable"]
                        if unresolved:
                            blockers.append({"code": "orchestration_program_unresolved_name", "diagnostics": unresolved})
                        if type_errors:
                            blockers.append({"code": "orchestration_program_type_error", "diagnostics": type_errors})
        for node_package_ref in allowed_node_package_refs:
            node_package = self._find_package(node_package_ref, run_id=run_id)
            if node_package is None:
                blockers.append({"code": "node_package_not_found", "package_ref": node_package_ref.resource_id})
                continue
            if run_id is not None and not self._package_visible_to_run(node_package, run_id):
                blockers.append({"code": "capability_package_scope_mismatch", "package_ref": self._package_ref(node_package)})
            try:
                await self._validate_package_exports(node_package)
            except (FileNotFoundError, TypeError, ValueError) as exc:
                blockers.append({
                    "code": "node_package_metadata_invalid",
                    "package_ref": self._package_ref(node_package),
                    "reason": str(exc),
                })
            for node_export in node_package.action_exports:
                try:
                    IoContract.model_validate(await self._load_json_content(node_export.io_contract_ref))
                except (FileNotFoundError, TypeError, ValueError):
                    blockers.append(
                        {
                            "code": "node_io_contract_unavailable",
                            "package_ref": self._package_ref(node_package),
                            "action_descriptor_ref": node_export.action_descriptor_ref.model_dump(mode="json"),
                        }
                    )
            if not any(self._slave_supports_package(slave_id, node_package) for slave_id in self.slave_capabilities):
                blockers.append({"code": "node_target_unavailable", "package_ref": self._package_ref(node_package)})
        return blockers


    async def _evaluate_readiness(self, snapshot: TaskClosure, *, run_id: str | None = None) -> dict[str, Any]:
        await self._load_workspace_packages()
        if run_id is not None:
            record = self.runs.get(run_id)
            if record is not None:
                workspace_id = record.closure_contract.workspace_id if record.closure_contract is not None else "workspace-default"
                await self.refresh_slaves(workspace_id)
        action_ref = snapshot.program.action_ref or snapshot.compute.action_ref
        action = action_name(action_ref)
        blockers: list[dict[str, Any]] = []
        inline_io_fields = [
            field_name
            for field_name in ("input_schema", "output_schema", "success_semantics")
            if getattr(snapshot.program, field_name, None) is not None
        ]
        if inline_io_fields:
            blockers.append(
                {
                    "code": "inline_io_contract_forbidden",
                    "fields": inline_io_fields,
                }
            )
        blockers.extend(await self._evaluate_input_schema(snapshot, action_ref))
        is_orchestration = (
            snapshot.program_systems.execution.kind,
            snapshot.program_systems.execution.version,
        ) == DRIVER_ORCHESTRATOR_KEY[1:]
        if is_orchestration:
            blockers.extend(await self._evaluate_orchestration_package(snapshot, run_id=run_id))
        bindings_by_hole = {binding.hole_id: binding for binding in snapshot.compute_bindings}

        for hole in snapshot.compute.typed_holes:
            binding = bindings_by_hole.get(hole.hole_id)
            if hole.status != "bound" or not hole.binding_ref:
                blockers.append({"code": "typed_hole_unbound", "hole_id": hole.hole_id})
                continue
            if binding is None or binding.binding_id != hole.binding_ref:
                blockers.append({"code": "compute_binding_missing", "hole_id": hole.hole_id, "binding_ref": hole.binding_ref})
                continue
            target = self._canonical_target_resource_id(binding.target_resource_ref.resource_id)
            capability = self.slave_capabilities.get(target)
            if capability is None:
                blockers.append({"code": "capability_unavailable", "hole_id": hole.hole_id, "target_resource_ref": target})
                continue
            if not self._slave_is_active(target):
                blockers.append({"code": "slave_unavailable", "hole_id": hole.hole_id, "target_resource_ref": target})
            package = self._find_package(binding.capability_package_ref, run_id=run_id) if binding.capability_package_ref else None
            if binding.capability_package_ref and package is None:
                foreign = self._find_package(binding.capability_package_ref)
                if foreign is not None:
                    blockers.append({"code": "capability_package_scope_mismatch", "hole_id": hole.hole_id, "source_run_ref": foreign.source_run_ref})
                else:
                    blockers.append({"code": "capability_package_not_found", "hole_id": hole.hole_id})
            elif package is not None:
                if package.is_interface:
                    blockers.append({"code": "capability_package_not_executable", "hole_id": hole.hole_id})
                    continue
                if run_id is not None and not self._package_visible_to_run(package, run_id):
                    blockers.append({"code": "capability_package_scope_mismatch", "hole_id": hole.hole_id, "source_run_ref": package.source_run_ref})
                if package.publication_state == "abandoned":
                    blockers.append({"code": "capability_package_abandoned", "package_ref": self._package_ref(package)})
                try:
                    action_export = package.export_for(binding.action_descriptor_ref)
                except ValueError:
                    blockers.append({"code": "action_descriptor_mismatch", "action": action})
                else:
                    if action_export.runtime_binding.get("interaction") == "byte-stream":
                        blockers.append({"code": "stream_action_not_task", "hole_id": hole.hole_id})
                    closure_contract_ref = snapshot.program.io_contract_ref
                    export_io_contract_ref = action_export.io_contract_ref
                    if (
                        closure_contract_ref is None
                        or (closure_contract_ref.digest or closure_contract_ref.resource_id)
                        != (export_io_contract_ref.digest or export_io_contract_ref.resource_id)
                    ):
                        blockers.append({"code": "io_contract_mismatch", "hole_id": hole.hole_id, "closure_io_contract_ref": closure_contract_ref.resource_id if closure_contract_ref else None, "package_io_contract_ref": export_io_contract_ref.resource_id})
                if not self._slave_supports_package(target, package):
                    blockers.append({"code": "runtime_provider_unavailable", "execution_kind": package.execution.kind})
            elif action == "run_code":
                # ``run_code`` is the generic subprocess executor, not an
                # executable builtin.  A concrete capability package is the
                # only source of program bytes and its IO contract, so a
                # binding without a package must be rejected during
                # readiness rather than discovered at dispatch time.
                blockers.append({"code": "capability_package_required", "hole_id": hole.hole_id})
            elif action and action not in capability.get("actions", set()):
                blockers.append({"code": "capability_unavailable", "action": action, "target_resource_ref": target})

        if action and not snapshot.compute.typed_holes and not is_orchestration:
            default_target = "slave-a"
            capability = self.slave_capabilities.get(default_target, {})
            if not self._slave_is_active(default_target):
                blockers.append({"code": "slave_unavailable", "target_resource_ref": default_target})
            if action == "run_code":
                blockers.append({"code": "capability_package_required", "target_resource_ref": default_target})
            elif action not in capability.get("actions", set()):
                blockers.append({"code": "capability_unavailable", "action": action, "target_resource_ref": default_target})

        return {
            "ready": not blockers,
            "action_ref": action_ref,
            "action": action,
            "blockers": blockers,
            "bindings": [binding.model_dump(mode="json") for binding in snapshot.compute_bindings],
            "orchestration": is_orchestration,
        }


    @staticmethod
    def _readiness_error(readiness: dict[str, Any]) -> DomainError:
        return DomainError(
            DomainErrorEnvelope(
                code="readiness_blocked",
                details={"blockers": deepcopy(readiness.get("blockers", []))},
            )
        )


    async def open_run(
        self,
        run_id: str | None,
        task_ref: str,
        goal: str,
        allow_reassignment: bool = False,
        closure_contract: ClosureContract | None = None,
        *,
        user_id: str = "user-default",
        workspace_id: str = "workspace-default",
    ) -> RunRecord:
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        if closure_contract is None:
            snapshot = TaskClosure.minimal(closure_id=task_ref, metadata={"goal": goal})
            closure_contract = ClosureContract(
                closure_id=task_ref,
                goal=goal,
                origin_conversation_ref=task_ref,
                user_id=user_id,
                workspace_id=workspace_id,
                resource_budget={"max_node_concurrency": 1},
                recovery_policy={"allow_reassignment": allow_reassignment},
                body=snapshot,
            )
        else:
            if closure_contract.goal != goal:
                raise ValueError("closure_goal_mismatch")
            snapshot = closure_contract.body.model_copy(deep=True)
            snapshot.closure_id = closure_contract.closure_id
            snapshot.metadata.setdefault("goal", goal)
            closure_contract = closure_contract.model_copy(
                update={
                    "origin_conversation_ref": task_ref,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "body": snapshot,
                },
                deep=True,
            )
        version = ClosureVersion(
            version_id=f"draft-{uuid4().hex[:12]}",
            closure_id=closure_contract.closure_id,
            snapshot=snapshot,
            snapshot_digest=snapshot.canonical_digest(),
            patch_cursor=0,
        )
        record = RunRecord(
            run_id=run_id,
            task_ref=task_ref,
            goal=goal,
            draft=version,
            closure_contract=closure_contract,
            allow_reassignment=allow_reassignment,
        )
        record.events.append({"phase": "run_opened", "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat()})
        self.runs[run_id] = record
        await self._persist(record)
        return record


    async def append_message(self, run_id: str, role: str, content: str, *, request_id: str | None = None) -> dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("unsupported_message_role")
        if not content:
            raise ValueError("empty_message")
        record = await self._load(run_id)
        if request_id:
            existing = next((event for event in record.events if event.get("phase") == "message" and event.get("request_id") == request_id), None)
            if existing is not None:
                return existing
        message = {
            "phase": "message",
            "message_id": f"message-{uuid4().hex[:12]}",
            "role": role,
            "content": content,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if request_id:
            message["request_id"] = request_id
        record.events.append(message)
        await self._persist(record)
        return message


    async def begin_refinement(self, run_id: str) -> RunRecord:
        record = await self._load(run_id)
        if record.state == "opened":
            _transition(record, "refinement_started")
            await self._persist(record)
        elif record.state not in {"thinking"}:
            raise ValueError("illegal_state_transition")
        return record


    async def inspect_readiness(self, run_id: str, version_id: str | None = None) -> dict[str, Any]:
        record = await self._load(run_id)
        if version_id is None or version_id == record.draft.version_id:
            snapshot = record.draft.snapshot
            inspected_version = record.draft.version_id
        elif record.committed is not None and version_id == record.committed.version_id:
            snapshot = record.committed.snapshot
            inspected_version = record.committed.version_id
        else:
            raise ValueError("version_conflict")
        readiness = await self._evaluate_readiness(snapshot, run_id=run_id)
        record.events.append(
            {
                "phase": "readiness_inspected",
                "run_id": run_id,
                "version": inspected_version,
                "ready": readiness["ready"],
                "blockers": readiness["blockers"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._persist(record)
        return {"run_id": run_id, "version": inspected_version, **readiness}


    @execution_transaction
    async def cancel_run(self, run_id: str, reason: str | dict[str, Any] = "user_interrupt") -> RunRecord:
        record = await self._load(run_id)
        if record.state == "cleaning_up":
            from loom_v2.pool.cleanup import continuation

            started = continuation(record)
            started["terminal_state"] = "cancelled"
            started["outcome"] = {"disposition": "cancelled", "reason": reason}
            await self._persist(record)
            return record
        if record.state in {"completed", "closed", "failed", "cancelled"}:
            return record
        _transition(record, "run_cancelled", reason=reason)
        record.outcome = {
            "disposition": "cancelled",
            "decision": None,
            "terminal_state": record.outcome.get("terminal_state") if isinstance(record.outcome, dict) else None,
            "terminal_error": reason,
            "reason": reason,
            "execution_id": record.execution_id,
            "execution_epoch": record.execution_epoch,
        }
        await self._persist(record)
        await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
        return record


    @execution_transaction
    async def fail_run(self, run_id: str, reason: str | dict[str, Any]) -> RunRecord:
        record = await self._load(run_id)
        if record.state == "cleaning_up":
            return record
        if record.state in {"completed", "closed", "cancelled", "failed"}:
            return record
        _transition(record, "run_failed", reason=reason)
        record.outcome = {
            "disposition": "failed",
            "decision": None,
            "terminal_state": None,
            "terminal_error": reason,
            "reason": reason,
            "execution_id": record.execution_id,
            "execution_epoch": record.execution_epoch,
        }
        await self._persist(record)
        await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
        return record


    async def record_agent_signal(self, run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist non-terminal app-server status/warning signals on a Run."""
        record = await self._load(run_id)
        event = {
            "phase": "coding_agent_signal",
            "run_id": run_id,
            "kind": kind,
            "payload": deepcopy(payload),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        record.events.append(event)
        await self._persist(record)
        return event


    async def _conversation_records(self, workspace_id: str, conversation_ref: str | None = None,
                                    *, summaries: bool = False) -> list:
        if self.sessions is None:
            return [r for r in self.runs.values() if (r.closure_contract is None or r.closure_contract.workspace_id == workspace_id)
                    and (conversation_ref is None or r.task_ref == conversation_ref)]
        predicates = [or_(RunRow.closure_contract["workspace_id"].as_string() == workspace_id,
                          RunRow.closure_contract["workspace_id"].as_string().is_(None))]
        if conversation_ref is not None:
            predicates.append(RunRow.task_ref == conversation_ref)
        async with self.sessions() as session:
            if summaries:
                from types import SimpleNamespace
                rows = (await session.execute(select(RunRow.run_id, RunRow.task_ref, RunRow.goal,
                                                     RunRow.state, RunRow.latest_event_at).where(*predicates))).all()
                return [SimpleNamespace(run_id=r.run_id, task_ref=r.task_ref, goal=r.goal, state=r.state,
                                        events=[{"created_at": r.latest_event_at}]) for r in rows]
            ids = (await session.scalars(select(RunRow.run_id).where(*predicates).order_by(RunRow.run_id))).all()
        return [await self._load(run_id) for run_id in ids]


    async def list_conversations(self, workspace_id: str = "workspace-default") -> list[dict[str, Any]]:
        groups: dict[str, list[RunRecord]] = {}
        for record in await self._conversation_records(workspace_id, summaries=True):
            if record.task_ref:
                groups.setdefault(record.task_ref, []).append(record)
        receipts = await self.list_message_receipts(workspace_id)
        receipt_groups: dict[str, list[MessageReceipt]] = {}
        for receipt in receipts:
            receipt_groups.setdefault(receipt.conversation_ref, []).append(receipt)
            groups.setdefault(receipt.conversation_ref, [])
        summaries: list[tuple[str, dict[str, Any]]] = []
        for conversation_ref, records in groups.items():
            records.sort(key=self._record_order_key)
            conversation_receipts = receipt_groups.get(conversation_ref, [])
            latest_receipt = conversation_receipts[-1] if conversation_receipts else None
            latest_record = records[-1] if records else None
            latest_key = latest_receipt.created_at.isoformat() if latest_receipt else self._record_order_key(latest_record) if latest_record else ""
            title = (latest_receipt.prompt if latest_receipt else latest_record.goal if latest_record else "New conversation") or "New conversation"
            status = self._conversation_status(records)
            if latest_receipt is not None and (latest_record is None or latest_receipt.updated_at.isoformat() >= self._record_order_key(latest_record)):
                status = self._message_receipt_status(latest_receipt.state)
            summaries.append(
                (
                    latest_key,
                    {
                        "conversation_ref": conversation_ref,
                        "title": title,
                        "run_count": len(records),
                        "latest_run_id": latest_record.run_id if latest_record else (latest_receipt.run_id if latest_receipt else None),
                        "status": status,
                    },
                )
            )
        return [summary for _, summary in sorted(summaries, key=lambda item: item[0])]


    async def get_conversation(self, conversation_ref: str, workspace_id: str = "workspace-default") -> dict[str, Any]:
        records = await self._conversation_records(workspace_id, conversation_ref)
        receipts = await self.list_message_receipts(workspace_id, conversation_ref)
        if not records and not receipts:
            raise KeyError(conversation_ref)
        records.sort(key=self._record_order_key)
        messages: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        run_summaries: list[dict[str, Any]] = []
        execution_items = await self.list_execution_requests(workspace_id, conversation_ref=conversation_ref, run_ids={r.run_id for r in records})
        execution_by_run: dict[str, ExecutionRequest] = {}
        for item in execution_items:
            if item.conversation_ref == conversation_ref or item.run_id in {record.run_id for record in records}:
                execution_by_run[item.run_id] = item
        receipt_run_ids = {receipt.run_id for receipt in receipts if receipt.run_id}
        receipts_by_run = {receipt.run_id: receipt for receipt in receipts if receipt.run_id}
        for receipt in receipts:
            messages.append(
                {
                    "message_id": f"receipt-{receipt.request_id}",
                    "role": "user",
                    "content": receipt.prompt,
                    "run_id": receipt.run_id,
                    "request_id": receipt.request_id,
                    "status": self._message_receipt_status(receipt.state),
                    "_projection_order": receipt.created_at.isoformat() + ":0",
                }
            )
            if receipt.assistant_text:
                messages.append(
                    {
                        "message_id": f"receipt-{receipt.request_id}-assistant",
                        "role": "assistant",
                        "content": receipt.assistant_text,
                        "run_id": receipt.run_id,
                        "request_id": receipt.request_id,
                        "_projection_order": receipt.created_at.isoformat() + ":1",
                    }
                )
        receipt_events = [
            deepcopy(event)
            for event in self.message_receipt_events
            if event.get("workspace_id") == workspace_id and event.get("conversation_ref") == conversation_ref
        ]
        known_receipts = {str(event.get("request_id")) for event in receipt_events}
        for receipt in receipts:
            if receipt.request_id not in known_receipts:
                receipt_events.append(
                    {
                        "phase": "message_receipt",
                        "workspace_id": workspace_id,
                        "request_id": receipt.request_id,
                        "conversation_ref": conversation_ref,
                        "state": receipt.state,
                        "created_at": receipt.updated_at.isoformat(),
                    }
                )
        events.extend(receipt_events)
        for record in records:
            has_user_message = False
            for event in record.events:
                events.append(event)
                if event.get("phase") != "message" or event.get("role") not in {"user", "assistant"}:
                    continue
                if record.run_id in receipt_run_ids:
                    continue
                message = {
                    "message_id": event.get("message_id", f"legacy-{record.run_id}"),
                    "role": event["role"],
                    "content": event.get("content", ""),
                    "run_id": event.get("run_id", record.run_id),
                    "_projection_order": str(event.get("created_at") or self._record_order_key(record)),
                }
                messages.append(message)
                has_user_message = has_user_message or message["role"] == "user"
            if not has_user_message and record.run_id not in receipt_run_ids:
                messages.append({"message_id": f"legacy-{record.run_id}", "role": "user", "content": record.goal, "run_id": record.run_id, "_projection_order": self._record_order_key(record)})
            run_summaries.append(
                {
                    "run_id": record.run_id,
                    "state": record.state,
                    "status": self._message_receipt_status(receipts_by_run[record.run_id].state) if record.run_id in receipts_by_run else self._conversation_status_for_state(record.state),
                    "committed_version": record.committed.version_id if record.committed else None,
                    "execution_id": record.execution_id,
                    "outcome": receipts_by_run.get(record.run_id).outcome if receipts_by_run.get(record.run_id) is not None and receipts_by_run[record.run_id].outcome is not None else record.outcome,
                    "receipt_state": receipts_by_run[record.run_id].state if record.run_id in receipts_by_run else None,
                    "dynamic_nodes": [node.model_dump(mode="json") for node in record.dynamic_nodes],
                    "capability_packages": [package.model_dump(mode="json") for package in record.capability_packages],
                    "capability_activations": [activation.model_dump(mode="json") for activation in record.capability_activations],
                    "execution_request": (
                        {
                            "request_id": execution_by_run[record.run_id].request_id,
                            "source": execution_by_run[record.run_id].source,
                            "state": execution_by_run[record.run_id].state,
                            "conversation_ref": execution_by_run[record.run_id].conversation_ref,
                        }
                        if record.run_id in execution_by_run else None
                    ),
                }
            )
            execution_item = execution_by_run.get(record.run_id)
            if execution_item is not None:
                events.append({
                    "phase": "execution_request",
                    "request_id": execution_item.request_id,
                    "run_id": record.run_id,
                    "source": execution_item.source,
                    "conversation_ref": execution_item.conversation_ref,
                    "state": execution_item.state,
                    "created_at": execution_item.created_at.isoformat(),
                })
        messages.sort(key=lambda item: str(item.get("_projection_order") or ""))
        for message in messages:
            message.pop("_projection_order", None)
        events.sort(key=lambda item: str(item.get("created_at") or ""))
        status = self._conversation_status(records)
        if receipts:
            latest_receipt = receipts[-1]
            latest_record = records[-1] if records else None
            if latest_record is None or latest_receipt.updated_at.isoformat() >= self._record_order_key(latest_record):
                status = self._message_receipt_status(latest_receipt.state)
        return {
            "conversation_ref": conversation_ref,
            "status": status,
            "messages": messages,
            "runs": run_summaries,
            "events": events,
        }


    async def apply_patch(
        self,
        run_id: str,
        base_draft_version: str,
        base_snapshot_digest: str,
        operation_id: str,
        ops: list[dict[str, Any]],
    ) -> PatchReceipt:
        if operation_id in self.idempotency:
            return self.idempotency[operation_id]
        record = await self._load(run_id)
        if record.draft.version_id != base_draft_version or record.draft.snapshot_digest != base_snapshot_digest:
            raise ValueError("version_conflict")
        if record.state not in {"opened", "thinking", "awaiting_decision"}:
            raise ValueError("illegal_state_transition")
        if record.state == "awaiting_decision" and (
            not isinstance(record.outcome, dict) or record.outcome.get("decision") != "repair"
        ):
            raise ValueError("illegal_state_transition")
        reopen = record.state == "awaiting_decision"
        begin = record.state == "opened"
        snapshot = record.draft.snapshot.model_copy(deep=True)
        for action in ops:
            # ``kind`` is the canonical wire field.  ``op``/``operation``
            # are accepted as equivalent spellings because JSON tool callers
            # commonly emit one of those names when translating a plan.
            kind = action.get("kind") or action.get("op") or action.get("operation")
            if kind == "set_result_expectation":
                snapshot.metadata["result_expectation"] = action.get("value", {})
            elif kind == "set_compute_spec":
                snapshot.compute = ComputeSpec.model_validate(action["value"])
            elif kind == "add_constraint":
                snapshot.constraints.append(Constraint.model_validate(action["value"]))
            elif kind == "tighten_constraint":
                value = action["value"]
                if not is_monotonic_tightening(value["parent"], value["child"]):
                    raise ValueError("constraint_not_monotonic")
                snapshot.metadata.setdefault("refined_constraints", []).append(value["child"])
            elif kind == "set_program_ref":
                value = action.get("value")
                if isinstance(value, dict):
                    value = value.get("program_ref") or value.get("action_ref") or value.get("ref")
                if not isinstance(value, str) or not value:
                    raise ValueError("invalid_program_ref")
                snapshot.program.action_ref = value
            elif kind == "set_io_contract_ref":
                value = action.get("value")
                if isinstance(value, dict) and "io_contract_ref" in value:
                    value = value["io_contract_ref"]
                try:
                    snapshot.program.io_contract_ref = ResourceRef.model_validate(value)
                except Exception as exc:
                    raise ValueError("io_contract_ref_required") from exc
            elif kind == "add_typed_hole":
                hole = TypedHole.model_validate(action["value"])
                if any(existing.hole_id == hole.hole_id for existing in snapshot.compute.typed_holes):
                    raise ValueError(f"duplicate_typed_hole:{hole.hole_id}")
                snapshot.compute.typed_holes.append(hole)
            elif kind == "materialize_capability_package_candidate":
                header = dict(action.get("value") or {})
                if set(header) - {"package_id", "package_version", "package_type", "execution", "action_exports", "body", "package_digest"}:
                    raise ValueError("capability_package_header_invalid")
                required = {"package_type", "execution", "action_exports"}
                if not required.issubset(header):
                    raise ValueError("capability_package_header_invalid")
                try:
                    package = CapabilityPackageVersion(
                        resource_authority=self.settings.grip_authority,
                        package_type=str(header["package_type"]),
                        package_id=str(header.get("package_id") or f"package-{uuid4().hex[:12]}"),
                        package_version=str(header.get("package_version") or "v1"),
                        package_closure_version_ref=f"package-closure-{uuid4().hex[:12]}",
                        source_run_ref=record.run_id,
                        source_closure_version_ref=record.draft.version_id,
                        workspace_id=(
                            record.closure_contract.workspace_id
                            if record.closure_contract is not None
                            else "workspace-default"
                        ),
                        execution=ExecutionContract.model_validate(header["execution"]),
                        action_exports=[
                            ActionExport.model_validate(item)
                            for item in header["action_exports"]
                        ],
                        body=header.get("body"),
                        package_digest=str(header.get("package_digest") or ""),
                        provenance=[{"source": "coding_agent", "run_id": record.run_id}],
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(str(exc) or "capability_package_invalid") from exc
                await self._validate_package_exports(package, publication=True)
                # Re-materializing the exact immutable package is idempotent,
                # but a logical coordinate must not silently change meaning
                # within one Run.  A caller that changes the program or any
                # other package field must choose a new version (or id).
                existing = next(
                    (
                        item
                        for item in record.capability_packages
                        if item.package_id == package.package_id
                        and item.package_version == package.package_version
                    ),
                    None,
                )
                if existing is None:
                    record.capability_packages.append(package)
                elif existing.package_digest != package.package_digest:
                    raise ValueError("capability_package_identity_conflict")
                else:
                    package = existing
                if (
                    package.package_type,
                    package.execution.kind,
                    package.execution.version,
                ) == DRIVER_ORCHESTRATOR_KEY:
                    snapshot.program_systems.execution = package.execution
                    snapshot.program_systems.package_ref = ResourceRef(resource_id=self._package_ref(package), version_or_digest=package.package_digest)
                    snapshot.program_systems.replay_safety = single_export(package).replay_safety
                snapshot.metadata.setdefault("capability_package_refs", []).append(self._package_ref(package))
            elif kind == "bind_compute_hole":
                value = action.get("value", {})
                if isinstance(value, dict) and "binding" in value:
                    value = value["binding"]
                binding = ComputeBinding.model_validate(value)
                hole = next((item for item in snapshot.compute.typed_holes if item.hole_id == binding.hole_id), None)
                if hole is None:
                    raise ValueError(f"typed_hole_not_found:{binding.hole_id}")
                snapshot.compute_bindings = [item for item in snapshot.compute_bindings if item.hole_id != binding.hole_id]
                snapshot.compute_bindings.append(binding)
                hole.status = "bound"
                hole.binding_ref = binding.binding_id
            elif kind == "set_execution_payload":
                value = action.get("value", {})
                if not isinstance(value, dict) or "input_ref" not in value:
                    raise ValueError("input_ref_required")
                try:
                    input_ref = ResourceRef.model_validate(value["input_ref"])
                except Exception as exc:
                    raise ValueError("input_ref_required") from exc
                node_id = str(value.get("node_id") or snapshot.program.action_ref or snapshot.compute.action_ref or "default")
                binding = NodeInputBinding(node_id=node_id, input_ref=input_ref, provenance=list(value.get("provenance", [])))
                snapshot.node_input_bindings = [item for item in snapshot.node_input_bindings if item.node_id != node_id]
                snapshot.node_input_bindings.append(binding)
            else:
                raise ValueError(f"unsupported_patch:{kind}")
        new_version = ClosureVersion(
            version_id=f"draft-{uuid4().hex[:12]}",
            closure_id=record.draft.closure_id,
            parent_version=record.draft.version_id,
            snapshot=snapshot,
            snapshot_digest=snapshot.canonical_digest(),
            patch_cursor=record.draft.patch_cursor + 1,
        )
        record.draft = new_version
        if begin:
            _transition(record, "refinement_started")
        elif reopen:
            _transition(record, "refinement_started")
            record.outcome = None
        receipt = PatchReceipt(
            receipt=f"receipt-{uuid4().hex[:12]}",
            run_id=run_id,
            kind="draft",
            draft_version=new_version.version_id,
            draft_digest=new_version.snapshot_digest,
            snapshot=snapshot,
            patch_cursor=new_version.patch_cursor,
        )
        receipt.readiness = await self._evaluate_readiness(snapshot, run_id=run_id)
        self.idempotency[operation_id] = receipt
        record.events.append({"phase": "draft_patched", "operation_id": operation_id, "version": new_version.version_id})
        package_refs = snapshot.metadata.get("capability_package_refs", [])
        if package_refs:
            record.events.append({"phase": "capability_package_candidate_materialized", "operation_id": operation_id, "package_refs": package_refs})
        if self.sessions is not None:
            async with self.sessions() as session:
                session.add(IdempotencyRow(operation_id=operation_id, receipt={"receipt": receipt.receipt, "run_id": receipt.run_id, "kind": receipt.kind, "draft_version": receipt.draft_version, "draft_digest": receipt.draft_digest, "patch_cursor": receipt.patch_cursor, "readiness": receipt.readiness}))
                await session.commit()
        await self._persist(record)
        return receipt


    async def commit(self, run_id: str, version_id: str, digest: str) -> ClosureVersion:
        record = await self._load(run_id)
        if record.state != "thinking":
            raise ValueError("illegal_state_transition")
        if record.draft.version_id != version_id or record.draft.snapshot_digest != digest:
            raise ValueError("version_conflict")
        readiness = await self._evaluate_readiness(record.draft.snapshot, run_id=run_id)
        record.events.append(
            {
                "phase": "readiness_inspected",
                "run_id": run_id,
                "version": record.draft.version_id,
                "ready": readiness["ready"],
                "blockers": readiness["blockers"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not readiness["ready"]:
            await self._persist(record)
            raise self._readiness_error(readiness)
        committed = record.draft.model_copy(update={"kind": "committed", "version_id": f"committed-{uuid4().hex[:12]}"})
        record.committed = committed
        _transition(record, "committed")
        record.events.append({"phase": "committed", "version": committed.version_id})
        await self._persist(record)
        return committed


    async def start(self, run_id: str, version_id: str) -> dict[str, Any]:
        record = await self._load(run_id)
        if record.state == "running" and record.execution_id is not None:
            if record.committed is None or record.committed.version_id != version_id:
                raise ValueError("closure_not_committed")
            return {"execution_id": record.execution_id, "state": record.state, "execution_epoch": record.execution_epoch}
        if record.state != "committed":
            raise ValueError("illegal_state_transition")
        if record.committed is None or record.committed.version_id != version_id:
            raise ValueError("closure_not_committed")
        readiness = await self._evaluate_readiness(record.committed.snapshot, run_id=run_id)
        record.events.append(
            {
                "phase": "readiness_inspected",
                "run_id": run_id,
                "version": record.committed.version_id,
                "ready": readiness["ready"],
                "blockers": readiness["blockers"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not readiness["ready"]:
            await self._persist(record)
            raise self._readiness_error(readiness)
        if (
            record.committed.snapshot.program_systems.execution.kind,
            record.committed.snapshot.program_systems.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY[1:]:
            self._check_attempt_budget(record)
        previous_execution_id = record.execution_id
        if previous_execution_id is None:
            record.execution_epoch = 1
        else:
            record.execution_epoch += 1
        record.execution_id = f"execution-{uuid4().hex[:12]}"
        record.outcome = None
        _transition(record, "execution_started")
        if (
            record.committed.snapshot.program_systems.execution.kind,
            record.committed.snapshot.program_systems.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY[1:]:
            target = "slave-a"
            if record.committed.snapshot.compute_bindings:
                target = self._canonical_target_resource_id(
                    record.committed.snapshot.compute_bindings[0].target_resource_ref.resource_id
                )
            record.attempts.append({"attempt_id": f"attempt-{uuid4().hex[:12]}", "target": target, "state": "created", "execution_epoch": record.execution_epoch})
        record.events.append(
            {
                "phase": "execution_started",
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
            }
        )
        if (
            record.committed.snapshot.program_systems.execution.kind,
            record.committed.snapshot.program_systems.execution.version,
        ) == DRIVER_ORCHESTRATOR_KEY[1:]:
            record.events.append(
                {
                    "phase": "orchestration_started",
                    "run_id": run_id,
                    "execution_id": record.execution_id,
                    "execution_epoch": record.execution_epoch,
                    "package_ref": record.committed.snapshot.program_systems.package_ref.model_dump(mode="json")
                    if record.committed.snapshot.program_systems.package_ref is not None
                    else None,
                    "provenance": self._orchestration_provenance(
                        record,
                        self._find_package(
                            record.committed.snapshot.program_systems.package_ref,
                            run_id=run_id,
                        )
                        if record.committed.snapshot.program_systems.package_ref is not None
                        else None,
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        await self._persist(record)
        return {"execution_id": record.execution_id, "state": record.state, "execution_epoch": record.execution_epoch}


    async def get_run(self, run_id: str) -> RunRecord:
        return await self._load(run_id)


    @execution_transaction
    async def record_result(self, run_id: str, result: dict[str, Any]) -> RunRecord:
        record = await self._load(run_id)
        if record.state != "running":
            raise ValueError("execution_not_running")

        # Terminal reports are scoped to the exact attempt and execution epoch
        # that the Pool created.  Never let a late retry (or a report for a
        # different attempt) mutate the current Run projection.
        attempt_id = result.get("attempt_id")
        attempt = next((item for item in record.attempts if item.get("attempt_id") == attempt_id), None)
        if attempt is None:
            raise ValueError("stale_attempt")
        try:
            execution_epoch = int(result.get("execution_epoch"))
        except (TypeError, ValueError) as exc:
            raise ValueError("stale_execution_epoch") from exc
        if execution_epoch != record.execution_epoch:
            raise ValueError("stale_execution_epoch")
        if attempt.get("execution_epoch", execution_epoch) != execution_epoch:
            raise ValueError("stale_execution_epoch")
        if attempt.get("state") not in {"created", "running"}:
            raise ValueError("stale_attempt")
        if result.get("execution_id") != record.execution_id:
            raise ValueError("stale_execution_id")

        if "value" not in result:
            raise ValueError("output_missing")
        if "digest" in result:
            raise ValueError("redundant_result_digest")
        expected_digest = self._result_digest(result["value"])
        raw_resource_ref = result.get("resource_ref")
        if raw_resource_ref is not None:
            try:
                resource_ref = ResourceRef.model_validate(raw_resource_ref)
            except Exception as exc:
                raise ValueError("invalid_resource_ref") from exc
            if resource_ref.digest != expected_digest:
                raise ValueError("resource_ref_digest_mismatch")

        # Validate Slave-produced evidence before accepting it.  Evidence is
        # descriptive, but it is still fenced to this attempt/epoch and to
        # the contract refs Observer resolved below.
        raw_evidence = result.get("validation_evidence") or []
        if not isinstance(raw_evidence, list):
            raise ValueError("invalid_validation_evidence")
        evidence: list[dict[str, Any]] = []
        for item in raw_evidence:
            try:
                parsed = ValidationEvidence.model_validate(item)
            except Exception as exc:
                raise ValueError("invalid_validation_evidence") from exc
            if parsed.attempt_id != attempt_id or parsed.execution_epoch != execution_epoch:
                raise ValueError("stale_validation_evidence")
            if parsed.issuer != "slave":
                raise ValueError("invalid_validation_evidence")
            output_digest = expected_digest
            if parsed.output_digest is not None and output_digest is not None and parsed.output_digest != output_digest:
                raise ValueError("validation_evidence_digest_mismatch")
            evidence.append(parsed.model_dump(mode="json"))

        snapshot = record.committed.snapshot if record.committed is not None else None
        contract: IoContract | None = None
        output_schema_ref: ResourceRef | None = None
        if snapshot is not None and snapshot.program.io_contract_ref is not None:
            try:
                contract = IoContract.model_validate(await self._load_json_content(snapshot.program.io_contract_ref))
            except (FileNotFoundError, TypeError, ValueError) as exc:
                raise ValueError("io_contract_invalid") from exc
            output_schema_ref = contract.output_schema_ref

        expected_input_digest = self._input_digest_for(snapshot) if snapshot is not None else None
        expected_validator_ref = contract.success_validator_ref if contract else None
        def _same_ref(left: ResourceRef | None, right: ResourceRef | None) -> bool:
            if left is None or right is None:
                return left is right
            return (left.digest or left.resource_id) == (right.digest or right.resource_id)

        for item in evidence:
            parsed_schema = ResourceRef.model_validate(item["schema_ref"]) if item.get("schema_ref") is not None else None
            parsed_validator = ResourceRef.model_validate(item["validator_ref"]) if item.get("validator_ref") is not None else None
            if output_schema_ref is not None and not _same_ref(parsed_schema, output_schema_ref):
                raise ValueError("validation_evidence_schema_mismatch")
            if not _same_ref(parsed_validator, expected_validator_ref):
                raise ValueError("validation_evidence_validator_mismatch")
            if item.get("input_digest") is not None and item.get("input_digest") != expected_input_digest:
                raise ValueError("validation_evidence_input_digest_mismatch")
        # Pool is the final task authority: it repeats output schema
        # validation even when a Slave supplied a pass evidence.  A mismatch
        # is a failed Attempt/ExecutionOutcome and a decision point for the
        # coding agent; it must never be projected as completed.
        pool_evidence: dict[str, Any] | None = None
        # Failed invocations have no successful output. Keep their original
        # terminal error, while still checking attempt/evidence identity above.
        if output_schema_ref is not None and result.get("terminal_state") != "failed":
            try:
                schema = await self._load_json_content(output_schema_ref)
                validate_schema(schema)
                schema_errors = validate(schema, result.get("value"))
            except (FileNotFoundError, TypeError, ValueError) as exc:
                schema_errors = [
                    SchemaValidationError(
                        path="$",
                        keyword="schema",
                        message="output schema could not be evaluated",
                        expected="valid output schema",
                        observed=str(exc),
                    )
                ]
            pool_evidence = ValidationEvidence(
                evidence_id=f"evidence-{attempt_id}-{uuid4().hex[:10]}",
                attempt_id=attempt_id,
                execution_epoch=execution_epoch,
                validator_ref=contract.success_validator_ref if contract else None,
                schema_ref=output_schema_ref,
                input_digest=self._input_digest_for(snapshot) if snapshot is not None else None,
                output_digest=expected_digest,
                result="fail" if schema_errors else "pass",
                errors=[item.model_dump(mode="json") for item in schema_errors],
                issuer="pool",
                created_at=datetime.now(timezone.utc).isoformat(),
            ).model_dump(mode="json")
            evidence.append(pool_evidence)

            if schema_errors:
                attempt["state"] = "failed"
                terminal_error = {"code": "output_schema_mismatch", "errors": pool_evidence["errors"]}
                outcome = {
                    **result,
                    "disposition": "awaiting_decision",
                    "decision": "repair",
                    "resource_ref": None,
                    "terminal_state": "failed",
                    "terminal_error": terminal_error,
                    "error": terminal_error,
                    "validation_evidence": evidence,
                }
                record.outcome = outcome
                _transition(record, "run_needs_decision", reason=terminal_error)
                record.events.append(
                    {
                        "phase": "io_schema_rejected",
                        "execution_id": record.execution_id,
                        "attempt_id": attempt_id,
                        "execution_epoch": execution_epoch,
                        "evidence_id": pool_evidence["evidence_id"],
                        "error": outcome["terminal_error"],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                await self._persist(record)
                return record

        validator_terminal_error: dict[str, Any] | None = None
        if expected_validator_ref is not None and result.get("terminal_state") != "failed":
            validator_errors: list[dict[str, Any]] = []
            validator_status = "fail"
            try:
                validator_program = await self.content_store.get(expected_validator_ref)
                validator_input = result["value"] if isinstance(result["value"], dict) else {"value": result["value"]}
                validator_result = await default_registry.invoke_for(
                    "function",
                    ExecutionContract(kind="process:json_stdio", version="1"),
                    validator_input,
                    program=validator_program,
                )
                validator_payload = validator_result.value if isinstance(validator_result.value, dict) else {}
                validator_status = str(validator_payload.get("result") or "fail")
                raw_errors = validator_payload.get("errors")
                if isinstance(raw_errors, list):
                    validator_errors = [
                        {
                            "path": str(item.get("path", "$")),
                            "keyword": str(item.get("keyword", "validator")),
                            "message": str(item.get("message", "validator failed"))[:512],
                        }
                        for item in raw_errors
                        if isinstance(item, dict)
                    ]
            except Exception as exc:
                validator_errors = [{"path": "$", "keyword": "validator", "message": str(exc)[:512]}]
            validator_evidence = ValidationEvidence(
                evidence_id=f"evidence-{attempt_id}-{uuid4().hex[:10]}",
                attempt_id=attempt_id,
                execution_epoch=execution_epoch,
                validator_ref=expected_validator_ref,
                schema_ref=output_schema_ref,
                input_digest=expected_input_digest,
                output_digest=expected_digest,
                result="pass" if validator_status == "pass" else "fail",
                errors=validator_errors,
                issuer="pool",
                created_at=datetime.now(timezone.utc).isoformat(),
            ).model_dump(mode="json")
            evidence.append(validator_evidence)
            if validator_status != "pass":
                validator_terminal_error = {"code": "success_validation_failed", "errors": validator_errors}

        terminal_state = str(result.get("terminal_state") or "completed")
        if terminal_state not in {"completed", "failed", "decision_required"}:
            raise ValueError("invalid_terminal_state")
        if validator_terminal_error is not None and terminal_state == "completed":
            terminal_state = "failed"
        terminal_error = validator_terminal_error or result.get("terminal_error")
        if terminal_state == "failed":
            attempt["state"] = "failed"
            record.outcome = {
                **result,
                "disposition": "awaiting_decision",
                "decision": "repair",
                "resource_ref": None,
                "terminal_state": "failed",
                "terminal_error": terminal_error,
                "error": terminal_error,
                "validation_evidence": evidence,
            }
            _transition(record, "run_needs_decision", reason=terminal_error)
            record.events.append(
                {
                    "phase": "io_schema_rejected" if terminal_error and terminal_error.get("code") in {"output_schema_mismatch", "success_validation_failed"} else "execution_failed",
                    "execution_id": record.execution_id,
                    "attempt_id": attempt_id,
                    "execution_epoch": execution_epoch,
                    "error": terminal_error,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        elif terminal_state == "decision_required":
            attempt["state"] = "decision_required"
            record.outcome = {
                **result,
                "disposition": "awaiting_decision",
                "decision": "attestation",
                "terminal_state": "decision_required",
                "terminal_error": None,
                "validation_evidence": evidence,
            }
            _transition(record, "run_needs_decision")
            record.events.append(
                {
                    "phase": "attestation_required",
                    "execution_id": record.execution_id,
                    "attempt_id": attempt_id,
                    "execution_epoch": execution_epoch,
                    "error": terminal_error,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        else:
            attempt["state"] = "completed"
            record.outcome = {
                **result,
                "disposition": "completed",
                "decision": None,
                "terminal_state": "completed",
                "validation_evidence": evidence,
            }
            _transition(record, "run_succeeded")
            record.events.append(
                {
                    "phase": "io_schema_validated" if pool_evidence is not None else "execution_completed",
                    "execution_id": record.execution_id,
                    "attempt_id": attempt_id,
                    "execution_epoch": execution_epoch,
                    "resource_ref": result.get("resource_ref"),
                    "evidence_id": pool_evidence["evidence_id"] if pool_evidence else None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        await self._persist(record)
        await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
        return record


    async def close_run(self, run_id: str) -> RunRecord:
        record = await self._load(run_id)
        if record.state not in {"completed", "failed", "cancelled"}:
            raise ValueError("run_not_terminal")
        _transition(record, "run_closed")
        record.events.append({"phase": "run_closed", "execution_id": record.execution_id})
        await self._persist(record)
        return record


    async def resolve_run(self, run_id: str, decision: str) -> RunRecord:
        record = await self._load(run_id)
        decision = str(decision)
        if record.state == "completed" and decision == "accept":
            return record
        if record.state == "failed" and decision == "abandon":
            return record
        if record.state != "awaiting_decision" or not isinstance(record.outcome, dict):
            raise ValueError("illegal_state_transition")
        pending_decision = record.outcome.get("decision")
        outcome = deepcopy(record.outcome)
        if decision == "accept":
            if pending_decision != "attestation":
                raise ValueError("invalid_decision")
            _transition(record, "decision_accepted")
            outcome["disposition"] = "completed"
            outcome["decision"] = None
            outcome["terminal_error"] = None
        elif decision == "abandon":
            _transition(record, "decision_abandoned")
            outcome["disposition"] = "failed"
            outcome["decision"] = None
        else:
            raise ValueError("invalid_decision")
        record.outcome = outcome
        record.events.append(
            {
                "phase": "run_decision_resolved",
                "decision": decision,
                "from_state": "awaiting_decision",
                "to_state": record.state,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._persist(record)
        return record


    def _orchestration_provenance(
        self,
        record: RunRecord,
        orchestration_package: CapabilityPackageVersion | None = None,
    ) -> dict[str, Any]:
        snapshot = record.committed.snapshot if record.committed is not None else record.draft.snapshot
        package_ref = snapshot.program_systems.package_ref
        package = orchestration_package
        if package is None and package_ref is not None:
            package = self._find_package(package_ref, run_id=record.run_id)
        return {
            "parent_execution_ref": record.execution_id,
            "closure_version_ref": record.committed.version_id if record.committed is not None else record.draft.version_id,
            "orchestration_package_ref": package_ref.model_dump(mode="json") if package_ref is not None else None,
            "orchestration_program_ref": package_body_ref(package, "program_content_ref").model_dump(mode="json") if package is not None else None,
        }


    @execution_transaction
    async def accept_node_intent(
        self,
        run_id: str,
        intent: NodeIntent,
        *,
        selected_target: str,
    ) -> DynamicNode:
        lock = self._dynamic_intent_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await self._accept_node_intent(run_id, intent, selected_target=selected_target)


    async def _accept_node_intent(
        self,
        run_id: str,
        intent: NodeIntent,
        *,
        selected_target: str,
    ) -> DynamicNode:
        """Deterministically materialize a program-emitted NodeIntent.

        This method never invokes an LLM and never creates new action or
        schema semantics.  It only validates the already-authorized package,
        content refs, limits, and target, then records a DynamicNode.
        """

        if not isinstance(intent, NodeIntent):
            intent = NodeIntent.model_validate(intent)
        record = await self._load(run_id)
        if record.state not in {"running", "cleaning_up"} or record.execution_id is None:
            raise ValueError("execution_not_running")
        if intent.execution_id != record.execution_id:
            raise ValueError("node_intent_execution_mismatch")
        from loom_v2.pool.cleanup import assert_cleanup_node, registrations

        assert_cleanup_node(record, intent)

        snapshot = record.committed.snapshot if record.committed is not None else record.draft.snapshot
        orchestration_ref = snapshot.program_systems.package_ref
        if orchestration_ref is None or (
            snapshot.program_systems.execution.kind,
            snapshot.program_systems.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY[1:]:
            raise ValueError("orchestration_package_not_bound")
        orchestration_package = self._find_package(orchestration_ref, run_id=run_id)
        if orchestration_package is None:
            foreign = self._find_package(orchestration_ref)
            if foreign is not None:
                raise ValueError("capability_package_scope_mismatch")
            raise ValueError("orchestration_package_not_found")
        if (
            orchestration_package.package_type,
            orchestration_package.execution.kind,
            orchestration_package.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY:
            raise ValueError("orchestration_package_not_found")
        try:
            allowed_node_package_refs = [
                ResourceRef.model_validate(item)
                for item in orchestration_package.body["allowed_node_package_refs"]
            ]
            max_nodes = int(orchestration_package.body["max_nodes"])
            max_live_nodes = int(orchestration_package.body["max_live_nodes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("orchestration_package_invalid") from exc

        if not any(
            event.get("phase") == "node_requested" and event.get("intent_id") == intent.intent_id
            for event in record.events
        ):
            record.events.append(
                {
                    "phase": "node_requested",
                    "run_id": run_id,
                    "execution_id": record.execution_id,
                    "execution_epoch": record.execution_epoch,
                    "intent_id": intent.intent_id,
                    "intent": intent.model_dump(mode="json"),
                    "provenance": self._orchestration_provenance(record, orchestration_package),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._persist(record)

        existing = next((node for node in record.dynamic_nodes if node.intent_id == intent.intent_id), None)
        if existing is not None:
            if (
                existing.package_ref == intent.package_ref
                and existing.action_descriptor_ref == intent.action_descriptor_ref
                and existing.input_refs == intent.input_refs
                and existing.instance_name == intent.instance_name
                and existing.target_resource_ref == intent.target_resource_ref
                and existing.activation_ref == intent.activation_ref
            ):
                return existing
            raise ValueError("node_intent_conflict")
        reserved = len(registrations(record)) if record.state == "running" else 0
        if len(record.dynamic_nodes) + reserved >= max_nodes:
            raise ValueError("orchestration_node_limit_exceeded")
        live_nodes = [node for node in record.dynamic_nodes if node.state not in {"completed", "failed", "decision_required"}]
        if len(live_nodes) >= max_live_nodes:
            raise ValueError("orchestration_live_node_limit_exceeded")

        def same_ref(left: ResourceRef, right: ResourceRef) -> bool:
            left_digest = (left.digest or "").lower()
            right_digest = (right.digest or "").lower()
            return left.resource_id == right.resource_id and left_digest == right_digest

        if not any(same_ref(intent.package_ref, allowed) for allowed in allowed_node_package_refs):
            raise ValueError("node_package_not_allowed")
        try:
            node_package = await self.get_capability_package(intent.package_ref, run_id=run_id)
        except KeyError as exc:
            raise ValueError("node_package_not_found") from exc
        if (intent.package_ref.digest or "").lower() != node_package.package_digest.lower():
            raise ValueError("node_package_digest_mismatch")
        if not self._package_visible_to_run(node_package, record.run_id):
            raise ValueError("capability_package_scope_mismatch")
        if node_package.publication_state == "abandoned":
            raise ValueError("capability_package_abandoned")
        try:
            node_export = node_package.export_for(intent.action_descriptor_ref)
        except ValueError as exc:
            raise ValueError("node_action_export_not_found") from exc
        if node_export.runtime_binding.get("interaction") == "byte-stream":
            raise ValueError("stream_action_not_task")

        workspace_id = record.closure_contract.workspace_id if record.closure_contract is not None else "workspace-default"
        await self.refresh_slaves(workspace_id)

        try:
            contract_payload = await self._load_json_content(node_export.io_contract_ref)
            contract = IoContract.model_validate(contract_payload)
        except (FileNotFoundError, TypeError, ValueError):
            raise ValueError("node_io_contract_unavailable")
        if contract.input_schema_ref is not None:
            try:
                schema = await self._load_json_content(contract.input_schema_ref)
                validate_schema(schema)
                values = [await self._load_json_content(input_ref) for input_ref in intent.input_refs]
                candidate_values = ([values[0]] if len(values) == 1 else [{"inputs": values}])
                errors: list[SchemaValidationError] = []
                for candidate in candidate_values:
                    errors = validate(schema, candidate)
                    if not errors:
                        break
                if errors:
                    raise ValueError(
                        "node_input_schema_mismatch:"
                        + json.dumps([item.model_dump(mode="json") for item in errors], sort_keys=True, ensure_ascii=False)
                    )
            except (FileNotFoundError, TypeError, ValueError) as exc:
                if str(exc).startswith("node_input_schema_mismatch"):
                    raise
                raise ValueError("node_input_schema_mismatch") from exc

        authorized_digests: set[str] = set()
        for event in record.events:
            if event.get("execution_id") != record.execution_id:
                continue
            if event.get("phase") == "program_content_materialized":
                authorized_digests.add(ResourceRef.model_validate(event["content_ref"]).digest)
            if event.get("phase") == "cleanup_registered" and record.state == "cleaning_up":
                authorized_digests.update(ResourceRef.model_validate(ref).digest for ref in event["input_refs"])
        for binding in snapshot.node_input_bindings:
            try:
                require_content_ref(binding.input_ref)
                authorized_digests.add((binding.input_ref.digest or "").lower())
                root_value = await self._load_json_content(binding.input_ref)
            except (FileNotFoundError, TypeError, ValueError):
                continue
            self._collect_content_ref_digests(root_value, authorized_digests)
        for attempt in record.attempts:
            if attempt.get("state") != "completed" or not attempt.get("result_ref"):
                continue
            try:
                result_ref = ResourceRef.model_validate(attempt["result_ref"])
                require_content_ref(result_ref)
                authorized_digests.add((result_ref.digest or "").lower())
            except (TypeError, ValueError):
                continue
        for input_ref in intent.input_refs:
            try:
                require_content_ref(input_ref)
                await self._load_json_content(input_ref)
            except (FileNotFoundError, TypeError, ValueError) as exc:
                raise ValueError("node_input_unavailable") from exc
            if (input_ref.digest or "").lower() not in authorized_digests:
                raise ValueError("node_input_not_allowed")

        if not isinstance(selected_target, str) or not selected_target:
            raise ValueError("node_target_unavailable")
        target = self._canonical_target_resource_id(selected_target)
        if not self._slave_supports_package(target, node_package):
            raise ValueError("node_target_unavailable")
        workspace_id = record.closure_contract.workspace_id if record.closure_contract is not None else "workspace-default"
        if intent.target_resource_ref is not None:
            await self.get_resource(workspace_id, intent.target_resource_ref)
        if intent.activation_ref is not None:
            activation_package_ref = str(intent.activation_ref.access_binding.get("package_version_ref") or intent.package_ref.resource_id)
            activation_package = await self.get_capability_package(activation_package_ref, run_id=run_id)
            activations = await self.list_package_activations(activation_package)
            expected_target = str(intent.activation_ref.access_binding.get("target_slave") or target)
            expected_instance = str(intent.activation_ref.access_binding.get("instance_name") or intent.instance_name)
            if activation_package.version_ref != node_package.version_ref or expected_target != target or expected_instance != intent.instance_name:
                raise ValueError("activation_binding_conflict")
            if not any(
                item.package_version_ref == activation_package.version_ref
                and item.package_digest == intent.activation_ref.digest
                and item.target_slave == expected_target
                and item.instance_name == expected_instance
                and item.activation_revision == intent.activation_ref.access_binding.get("activation_revision")
                and item.activation_state == "ready"
                and (intent.target_resource_ref is None or item.target_resource_ref == intent.target_resource_ref)
                for item in activations
            ):
                raise ValueError("activation_not_ready")
        if record.closure_contract is not None and node_export.permissions:
            if not set(node_export.permissions).issubset(set(record.closure_contract.allowed_effects)):
                raise ValueError("node_permission_denied")
        locality = next(
            (
                requirement.value
                for requirement in snapshot.compute.requirements
                if requirement.key == "loom.data.locality.v1"
            ),
            None,
        )
        if locality is not None and self._canonical_target_resource_id(str(locality)) != target:
            raise ValueError("node_target_unavailable")

        if len(record.dynamic_nodes) >= max_nodes:
            raise ValueError("orchestration_node_limit_exceeded")
        live_nodes = [node for node in record.dynamic_nodes if node.state not in {"completed", "failed", "decision_required"}]
        if len(live_nodes) >= max_live_nodes:
            raise ValueError("orchestration_live_node_limit_exceeded")

        node_id = f"node-{record.execution_id}-{len(record.dynamic_nodes) + 1}"
        node = DynamicNode(
            node_id=node_id,
            parent_execution_ref=record.execution_id,
            intent_id=intent.intent_id,
            package_ref=intent.package_ref,
            action_descriptor_ref=intent.action_descriptor_ref,
            input_refs=intent.input_refs,
            instance_name=intent.instance_name,
            target_resource_ref=intent.target_resource_ref,
            activation_ref=intent.activation_ref,
            state="accepted",
        )
        record.dynamic_nodes.append(node)
        provenance = self._orchestration_provenance(record, orchestration_package)
        record.events.extend(
            [
                {
                    "phase": "node_accepted",
                    "run_id": run_id,
                    "execution_id": record.execution_id,
                    "execution_epoch": record.execution_epoch,
                    "node_id": node.node_id,
                    "intent_id": intent.intent_id,
                    "package_ref": intent.package_ref.model_dump(mode="json"),
                    "action_descriptor_ref": intent.action_descriptor_ref.model_dump(mode="json"),
                    "input_refs": [item.model_dump(mode="json") for item in intent.input_refs],
                    "instance_name": intent.instance_name,
                    "selected_target": target,
                    "reason": "capability_match",
                    "provenance": {
                        **provenance,
                        "node_package_ref": node.package_ref.model_dump(mode="json"),
                        "action_descriptor_ref": node.action_descriptor_ref.model_dump(mode="json"),
                    },
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            ]
        )
        await self._persist(record)
        return node


    @execution_transaction
    async def dispatch_dynamic_node(self, run_id: str, node_id: str, *, target: str) -> dict[str, Any]:
        record = await self._load(run_id)
        workspace_id = record.closure_contract.workspace_id if record.closure_contract is not None else "workspace-default"
        if record.state not in {"running", "cleaning_up"} or record.execution_id is None:
            raise ValueError("execution_not_running")
        node = next((item for item in record.dynamic_nodes if item.node_id == node_id), None)
        if node is None:
            raise ValueError("dynamic_node_not_found")
        from loom_v2.pool.cleanup import assert_cleanup_node

        assert_cleanup_node(record, node)
        if node.state != "accepted":
            raise ValueError("dynamic_node_not_dispatchable")
        if not isinstance(target, str) or not target:
            raise ValueError("node_target_unavailable")
        await self.refresh_slaves(workspace_id)
        normalized_target = self._canonical_target_resource_id(target)
        accepted_event = next(
            (
                event
                for event in reversed(record.events)
                if event.get("phase") in {"node_accepted", "node_reassigned"} and event.get("node_id") == node_id
            ),
            None,
        )
        accepted_target = accepted_event.get("selected_target") if accepted_event is not None else None
        if accepted_target is None and accepted_event is not None:
            accepted_target = accepted_event.get("target")
        if accepted_target is not None and accepted_target != normalized_target:
            raise ValueError("node_target_mismatch")
        node_package = self._find_package(node.package_ref, run_id=run_id)
        if node_package is None or not self._slave_supports_package(normalized_target, node_package):
            raise ValueError("node_target_unavailable")
        try:
            node_package.export_for(node.action_descriptor_ref)
        except ValueError as exc:
            raise ValueError("node_action_export_not_found") from exc
        target_agent = self.slave_agents[normalized_target]
        self._check_attempt_budget(record)
        attempt = {
            "attempt_id": f"attempt-{uuid4().hex[:12]}",
            "node_id": node_id,
            "target": normalized_target,
            "target_instance_id": target_agent["instance_id"],
            "target_agent_epoch": int(target_agent["epoch"]),
            "state": "created",
            "execution_epoch": record.execution_epoch,
            "replaces_attempt_id": None,
            "replaced_by_attempt_id": None,
        }
        record.attempts.append(attempt)
        node.state = "dispatched"
        record.events.append(
            {
                "phase": "node_dispatched",
                "run_id": run_id,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "node_id": node_id,
                "attempt_id": attempt["attempt_id"],
                "target": normalized_target,
                "target_instance_id": attempt["target_instance_id"],
                "target_agent_epoch": attempt["target_agent_epoch"],
                "package_ref": node.package_ref.model_dump(mode="json"),
                "action_descriptor_ref": node.action_descriptor_ref.model_dump(mode="json"),
                "input_refs": [item.model_dump(mode="json") for item in node.input_refs],
                "provenance": {
                    **self._orchestration_provenance(record),
                    "node_package_ref": node.package_ref.model_dump(mode="json"),
                    "action_descriptor_ref": node.action_descriptor_ref.model_dump(mode="json"),
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._persist(record)
        return deepcopy(attempt)


    @execution_transaction
    async def reassign_dynamic_node(
        self,
        run_id: str,
        node_id: str,
        *,
        lost_attempt_id: str,
        expected_execution_id: str,
        expected_execution_epoch: int,
        target: str,
        reason: str,
    ) -> dict[str, Any]:
        lock = self._dynamic_intent_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            record = await self._load(run_id)
            if record.state not in {"running", "cleaning_up"} or record.execution_id is None:
                raise ValueError("execution_not_running")
            if expected_execution_id != record.execution_id:
                raise ValueError("stale_execution_id")
            if int(expected_execution_epoch) != record.execution_epoch:
                raise ValueError("stale_execution_epoch")
            node = next((item for item in record.dynamic_nodes if item.node_id == node_id), None)
            if node is None:
                raise ValueError("dynamic_node_not_found")
            old_attempt = next(
                (
                    item
                    for item in record.attempts
                    if item.get("attempt_id") == lost_attempt_id and item.get("node_id") == node_id
                ),
                None,
            )
            if old_attempt is None:
                raise ValueError("stale_attempt")
            replacement_id = old_attempt.get("replaced_by_attempt_id")
            if replacement_id:
                replacement = next(
                    (item for item in record.attempts if item.get("attempt_id") == replacement_id),
                    None,
                )
                if replacement is None:
                    raise ValueError("stale_attempt")
                return deepcopy(replacement)
            if node.state != "dispatched":
                raise ValueError("dynamic_node_not_dispatchable")
            active_attempts = [
                item
                for item in record.attempts
                if item.get("node_id") == node_id and item.get("state") in {"created", "running"}
            ]
            if active_attempts != [old_attempt]:
                raise ValueError("stale_attempt")
            if old_attempt.get("execution_epoch") != record.execution_epoch:
                raise ValueError("stale_attempt")
            policy = record.closure_contract.recovery_policy if record.closure_contract is not None else {}
            if not bool(policy.get("allow_reassignment", False)):
                raise ValueError("reassignment_not_allowed")

            package = self._find_package(node.package_ref, run_id=run_id)
            if package is None:
                raise ValueError("node_target_unavailable")
            try:
                action_export = package.export_for(node.action_descriptor_ref)
            except ValueError as exc:
                raise ValueError("node_action_export_not_found") from exc
            if record.closure_contract is not None and action_export.permissions:
                if not set(action_export.permissions).issubset(set(record.closure_contract.allowed_effects)):
                    raise ValueError("node_permission_denied")
            for input_ref in node.input_refs:
                try:
                    require_content_ref(input_ref)
                    await self._load_json_content(input_ref)
                except (FileNotFoundError, TypeError, ValueError) as exc:
                    raise ValueError("node_input_unavailable") from exc
            self._check_attempt_budget(record)

            workspace_id = record.closure_contract.workspace_id if record.closure_contract is not None else "workspace-default"
            await self.refresh_slaves(workspace_id)
            source_key = (
                str(old_attempt.get("target") or ""),
                str(old_attempt.get("target_instance_id") or ""),
            )
            source_agent = self.slave_instances.get(source_key)
            if source_agent is None or int(source_agent.get("epoch") or 0) != int(old_attempt.get("target_agent_epoch") or 0):
                raise ValueError("worker_lease_not_lost")
            if source_agent.get("lease_state") not in {"expired", "released"}:
                raise ValueError("worker_lease_active")
            normalized_target = self._canonical_target_resource_id(target)
            if (
                not normalized_target
                or normalized_target == old_attempt.get("target")
                or not self._slave_supports_package(normalized_target, package)
            ):
                raise ValueError("node_target_unavailable")
            snapshot = record.committed.snapshot if record.committed is not None else record.draft.snapshot
            locality = next(
                (
                    requirement.value
                    for requirement in snapshot.compute.requirements
                    if requirement.key == "loom.data.locality.v1"
                ),
                None,
            )
            if locality is not None and self._canonical_target_resource_id(str(locality)) != normalized_target:
                raise ValueError("node_target_unavailable")
            target_agent = self.slave_agents[normalized_target]
            updated = deepcopy(record)
            updated_old_attempt = next(
                item for item in updated.attempts if item.get("attempt_id") == lost_attempt_id
            )
            updated_node = next(item for item in updated.dynamic_nodes if item.node_id == node_id)
            replacement = {
                "attempt_id": f"attempt-{uuid4().hex[:12]}",
                "node_id": node_id,
                "target": normalized_target,
                "target_instance_id": target_agent["instance_id"],
                "target_agent_epoch": int(target_agent["epoch"]),
                "state": "created",
                "execution_epoch": record.execution_epoch,
                "replaces_attempt_id": old_attempt["attempt_id"],
                "replaced_by_attempt_id": None,
            }
            updated_old_attempt["state"] = "lost"
            updated_old_attempt["terminal_error"] = {
                "code": "worker_lost",
                "lease_state": source_agent["lease_state"],
            }
            updated_old_attempt["replaced_by_attempt_id"] = replacement["attempt_id"]
            updated.attempts.append(replacement)
            updated_node.state = "dispatched"
            contract = updated.closure_contract
            updated.events.append(
                {
                    "phase": "node_reassigned",
                    "run_id": run_id,
                    "execution_id": updated.execution_id,
                    "execution_epoch": updated.execution_epoch,
                    "node_id": node_id,
                    "lost_attempt_id": updated_old_attempt["attempt_id"],
                    "replacement_attempt_id": replacement["attempt_id"],
                    "from_target": updated_old_attempt["target"],
                    "from_instance_id": updated_old_attempt["target_instance_id"],
                    "from_agent_epoch": updated_old_attempt["target_agent_epoch"],
                    "to_target": replacement["target"],
                    "to_instance_id": replacement["target_instance_id"],
                    "to_agent_epoch": replacement["target_agent_epoch"],
                    "reason": reason,
                    "source_lease_state": source_agent["lease_state"],
                    "reassignment_authorization": {
                        "policy": "allow_reassignment",
                        "value": True,
                        "closure_version_ref": updated.committed.version_id if updated.committed is not None else updated.draft.version_id,
                        "user_id": contract.user_id if contract is not None else "user-default",
                        "origin_conversation_ref": contract.origin_conversation_ref if contract is not None else updated.task_ref,
                    },
                    "package_ref": updated_node.package_ref.model_dump(mode="json"),
                    "package_replay_safety": action_export.replay_safety,
                    "input_refs": [item.model_dump(mode="json") for item in updated_node.input_refs],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._persist(updated)
            self.runs[run_id] = updated
            return deepcopy(replacement)


    @execution_transaction
    async def record_dynamic_node_result(self, run_id: str, node_id: str, result: dict[str, Any]) -> DynamicNode:
        record = await self._load(run_id)
        if record.state not in {"running", "cleaning_up"} or record.execution_id is None:
            raise ValueError("execution_not_running")
        if not isinstance(result, dict):
            raise ValueError("invalid_result")
        node = next((item for item in record.dynamic_nodes if item.node_id == node_id), None)
        if node is None:
            raise ValueError("dynamic_node_not_found")
        from loom_v2.pool.cleanup import assert_cleanup_node

        assert_cleanup_node(record, node)
        if result.get("execution_id") != record.execution_id:
            raise ValueError("stale_execution_id")
        try:
            execution_epoch = int(result.get("execution_epoch"))
        except (TypeError, ValueError) as exc:
            raise ValueError("stale_execution_epoch") from exc
        if execution_epoch != record.execution_epoch:
            raise ValueError("stale_execution_epoch")
        attempt_id = result.get("attempt_id")
        attempt = next(
            (
                item
                for item in reversed(record.attempts)
                if item.get("attempt_id") == attempt_id and item.get("node_id") == node_id
            ),
            None,
        )
        if attempt is None or attempt.get("execution_epoch", execution_epoch) != execution_epoch:
            raise ValueError("stale_attempt")
        if attempt.get("state") not in {"created", "running"}:
            raise ValueError("stale_attempt")
        if "value" not in result:
            raise ValueError("output_missing")
        if "digest" in result:
            raise ValueError("redundant_result_digest")
        value = result["value"]
        expected_digest = self._result_digest(value)
        reported_terminal_state = str(result.get("terminal_state") or "completed")
        if reported_terminal_state not in {"completed", "failed", "decision_required"}:
            raise ValueError("invalid_terminal_state")

        contract: IoContract | None = None
        output_schema_ref: ResourceRef | None = None
        raw_validation_evidence = result.get("validation_evidence")
        if raw_validation_evidence is None:
            validation_evidence = []
        elif not isinstance(raw_validation_evidence, list):
            raise ValueError("invalid_validation_evidence")
        else:
            validation_evidence = []
            for item in raw_validation_evidence:
                try:
                    parsed_evidence = ValidationEvidence.model_validate(item)
                except Exception as exc:
                    raise ValueError("invalid_validation_evidence") from exc
                if parsed_evidence.attempt_id != attempt_id or parsed_evidence.execution_epoch != execution_epoch:
                    raise ValueError("stale_validation_evidence")
                if parsed_evidence.issuer != "slave":
                    raise ValueError("invalid_validation_evidence")
                if parsed_evidence.output_digest is not None and parsed_evidence.output_digest != expected_digest:
                    raise ValueError("validation_evidence_digest_mismatch")
                validation_evidence.append(parsed_evidence.model_dump(mode="json"))
        try:
            node_package = await self.get_capability_package(node.package_ref, run_id=run_id)
        except KeyError as exc:
            raise ValueError("node_package_not_found") from exc
        if node_package.package_digest != (node.package_ref.digest or ""):
            raise ValueError("node_package_digest_mismatch")
        try:
            node_export = node_package.export_for(node.action_descriptor_ref)
            contract = IoContract.model_validate(await self._load_json_content(node_export.io_contract_ref))
            output_schema_ref = contract.output_schema_ref
            def same_ref(left: ResourceRef | None, right: ResourceRef | None) -> bool:
                if left is None or right is None:
                    return left is right
                return (left.digest or left.resource_id) == (right.digest or right.resource_id)

            for item in validation_evidence:
                evidence_schema = ResourceRef.model_validate(item["schema_ref"]) if item.get("schema_ref") is not None else None
                evidence_validator = ResourceRef.model_validate(item["validator_ref"]) if item.get("validator_ref") is not None else None
                if not same_ref(evidence_schema, output_schema_ref):
                    raise ValueError("validation_evidence_schema_mismatch")
                if not same_ref(evidence_validator, contract.success_validator_ref):
                    raise ValueError("validation_evidence_validator_mismatch")
            schema_errors: list[SchemaValidationError] = []
            if output_schema_ref is not None and reported_terminal_state != "failed":
                schema = await self._load_json_content(output_schema_ref)
                validate_schema(schema)
                schema_errors = validate(schema, value)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            if str(exc).startswith("validation_evidence_"):
                raise
            schema_errors = [
                SchemaValidationError(
                    path="$",
                    keyword="schema",
                    message="node output schema could not be evaluated",
                    expected="valid output schema",
                    observed=str(exc),
                )
            ]

        if reported_terminal_state != "failed" and not schema_errors and contract is not None and contract.success_validator_ref is not None:
            validator_errors: list[dict[str, Any]] = []
            validator_status = "fail"
            try:
                validator_program = await self.content_store.get(contract.success_validator_ref)
                validator_input = value if isinstance(value, dict) else {"value": value}
                validator_result = await default_registry.invoke_for(
                    "function",
                    ExecutionContract(kind="process:json_stdio", version="1"),
                    validator_input,
                    program=validator_program,
                )
                validator_payload = validator_result.value if isinstance(validator_result.value, dict) else {}
                validator_status = str(validator_payload.get("result") or "fail")
                raw_errors = validator_payload.get("errors")
                if isinstance(raw_errors, list):
                    validator_errors = [item for item in raw_errors if isinstance(item, dict)]
            except Exception as exc:
                validator_errors = [{"path": "$", "keyword": "validator", "message": str(exc)[:512]}]
            validation_evidence.append(
                ValidationEvidence(
                    evidence_id=f"evidence-{attempt_id}-{uuid4().hex[:10]}",
                    attempt_id=attempt_id,
                    execution_epoch=execution_epoch,
                    validator_ref=contract.success_validator_ref,
                    schema_ref=output_schema_ref,
                    output_digest=expected_digest,
                    result="pass" if validator_status == "pass" and not validator_errors else "fail",
                    errors=validator_errors,
                    issuer="pool",
                    created_at=datetime.now(timezone.utc).isoformat(),
                ).model_dump(mode="json")
            )
            if validator_status != "pass" or validator_errors:
                schema_errors.append(
                    SchemaValidationError(
                        path="$",
                        keyword="validator",
                        message="node output validator failed",
                        expected="validator pass",
                        observed=json.dumps(validator_errors, ensure_ascii=False),
                    )
                )

        result_ref = await self.content_store.put(canonical_json_bytes(value), media_type="application/json")
        if schema_errors:
            terminal_state = "failed"
            terminal_error = {
                "code": "success_validation_failed" if any(item.keyword == "validator" for item in schema_errors) else "output_schema_mismatch",
                "errors": [item.model_dump(mode="json") for item in schema_errors],
            }
        else:
            terminal_state = reported_terminal_state
            terminal_error = result.get("terminal_error") if isinstance(result.get("terminal_error"), dict) else None
        attempt["state"] = terminal_state
        attempt["result_ref"] = result_ref.model_dump(mode="json")
        attempt["value"] = value
        attempt["terminal_error"] = terminal_error
        attempt["validation_evidence"] = validation_evidence
        node.state = terminal_state
        if terminal_state in {"failed", "decision_required"}:
            decision = "attestation" if terminal_state == "decision_required" else "repair"
            record.outcome = {
                "disposition": "awaiting_decision",
                "decision": decision,
                "terminal_state": terminal_state,
                "terminal_error": terminal_error,
                "resource_ref": result_ref.model_dump(mode="json") if decision == "attestation" else None,
                "value": value,
                "node_id": node_id,
                "attempt_id": attempt_id,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
            }
            _transition(record, "run_needs_decision", reason=terminal_error)
        record.events.append(
            {
                "phase": "node_completed",
                "run_id": run_id,
                "execution_id": record.execution_id,
                "node_id": node_id,
                "attempt_id": attempt_id,
                "target": attempt.get("target"),
                "execution_epoch": execution_epoch,
                "package_ref": node.package_ref.model_dump(mode="json"),
                "input_refs": [item.model_dump(mode="json") for item in node.input_refs],
                "result_ref": result_ref.model_dump(mode="json"),
                "terminal_state": terminal_state,
                "terminal_error": terminal_error,
                "validation_evidence": validation_evidence,
                "provenance": {
                    **self._orchestration_provenance(record),
                    "node_package_ref": node.package_ref.model_dump(mode="json"),
                    "attempt_id": attempt_id,
                    "target": attempt.get("target"),
                    "slave_id": attempt.get("target"),
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._persist(record)
        if terminal_state in {"failed", "decision_required"} and record.state != "cleaning_up":
            await self._settle_execution_request(run_id, execution_id=record.execution_id, outcome=record.outcome)
        return node


    @execution_transaction
    async def fail_dynamic_node(
        self,
        run_id: str,
        node_id: str,
        *,
        attempt_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> DynamicNode:
        record = await self._load(run_id)
        node = next((item for item in record.dynamic_nodes if item.node_id == node_id), None)
        if node is None:
            raise ValueError("dynamic_node_not_found")
        if error is not None and not isinstance(error, dict):
            raise ValueError("invalid_error")
        reason = error or {"code": "dynamic_node_failed"}
        if attempt_id is not None:
            requested_attempt = next(
                (item for item in record.attempts if item.get("attempt_id") == attempt_id and item.get("node_id") == node_id),
                None,
            )
            if (
                requested_attempt is None
                or requested_attempt.get("execution_epoch", record.execution_epoch) != record.execution_epoch
                or requested_attempt.get("state") not in {"created", "running"}
            ):
                raise ValueError("stale_attempt")
        node.state = "failed"
        failed_attempt = None
        for attempt in record.attempts:
            if attempt.get("node_id") == node_id and (attempt_id is None or attempt.get("attempt_id") == attempt_id):
                failed_attempt = attempt
                if attempt.get("state") in {"created", "running"}:
                    attempt["state"] = "failed"
                    attempt["terminal_error"] = reason
        _transition(record, "run_needs_decision", reason=reason)
        record.outcome = {
            "disposition": "awaiting_decision",
            "decision": "repair",
            "terminal_state": "failed",
            "terminal_error": reason,
            "resource_ref": None,
            "execution_id": record.execution_id,
            "execution_epoch": record.execution_epoch,
        }
        record.events.append(
            {
                "phase": "node_completed",
                "run_id": run_id,
                "execution_id": record.execution_id,
                "node_id": node_id,
                "attempt_id": attempt_id,
                "target": failed_attempt.get("target") if failed_attempt is not None else None,
                "execution_epoch": record.execution_epoch,
                "package_ref": node.package_ref.model_dump(mode="json"),
                "input_refs": [item.model_dump(mode="json") for item in node.input_refs],
                "terminal_state": "failed",
                "terminal_error": reason,
                "provenance": {
                    **self._orchestration_provenance(record),
                    "node_package_ref": node.package_ref.model_dump(mode="json"),
                    "attempt_id": attempt_id,
                    "target": failed_attempt.get("target") if failed_attempt is not None else None,
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        record.events.append(
            {
                "phase": "orchestration_failed",
                "run_id": run_id,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "node_id": node_id,
                "error": reason,
                "provenance": self._orchestration_provenance(record),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._persist(record)
        if record.state != "cleaning_up":
            await self._settle_execution_request(run_id, execution_id=record.execution_id, outcome=record.outcome)
        return node


    @execution_transaction
    async def complete_orchestration(self, run_id: str, final_ref: ResourceRef) -> RunRecord:
        record = await self._load(run_id)
        if record.state != "running" or record.execution_id is None:
            raise ValueError("execution_not_running")
        snapshot = record.committed.snapshot if record.committed is not None else record.draft.snapshot
        if (
            snapshot.program_systems.execution.kind,
            snapshot.program_systems.execution.version,
        ) != DRIVER_ORCHESTRATOR_KEY[1:]:
            raise ValueError("orchestration_not_running")
        if any(node.state in {"failed", "decision_required"} for node in record.dynamic_nodes):
            raise ValueError("dynamic_nodes_not_successful")
        if any(node.state != "completed" for node in record.dynamic_nodes):
            raise ValueError("dynamic_nodes_not_terminal")
        contract: IoContract | None = None
        value: Any = None
        final_loaded = False
        schema_errors: list[SchemaValidationError] = []
        if snapshot.program.io_contract_ref is None:
            raise ValueError("io_contract_unavailable")
        try:
            value = await self._load_json_content(final_ref)
            final_loaded = True
            contract = IoContract.model_validate(await self._load_json_content(snapshot.program.io_contract_ref))
            output_schema_ref = contract.output_schema_ref
            if output_schema_ref is not None:
                schema = await self._load_json_content(output_schema_ref)
                validate_schema(schema)
                schema_errors = validate(schema, value)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            schema_errors = [
                SchemaValidationError(
                    path="$",
                    keyword="schema",
                    message="final output schema could not be evaluated",
                    expected="valid final output",
                    observed=str(exc),
                )
            ]
        digest = self._result_digest(value) if final_loaded else ""
        if final_loaded and (final_ref.digest or "").lower() != digest:
            schema_errors.append(
                SchemaValidationError(
                    path="$",
                    keyword="digest",
                    message="final ref digest mismatch",
                    expected=digest,
                    observed=final_ref.digest,
                )
            )
        orchestration_package = self._find_package(snapshot.program_systems.package_ref, run_id=run_id) if snapshot.program_systems.package_ref else None
        orchestration_provenance = self._orchestration_provenance(record, orchestration_package)
        lineage: list[dict[str, Any]] = []
        completed_digests: set[str] = set()
        for node in record.dynamic_nodes:
            attempt = next(
                (
                    item
                    for item in reversed(record.attempts)
                    if item.get("node_id") == node.node_id
                    and item.get("state") == "completed"
                    and item.get("result_ref")
                ),
                None,
            )
            if attempt is None:
                continue
            result_ref = ResourceRef.model_validate(attempt["result_ref"])
            completed_digests.add((result_ref.digest or "").lower())
            lineage.append(
                {
                    "node_id": node.node_id,
                    "intent_id": node.intent_id,
                    "package_ref": node.package_ref.model_dump(mode="json"),
                    "input_refs": [item.model_dump(mode="json") for item in node.input_refs],
                    "attempt_id": attempt.get("attempt_id"),
                    "target": attempt.get("target"),
                    "execution_epoch": attempt.get("execution_epoch", record.execution_epoch),
                    "result_ref": attempt["result_ref"],
                    "provenance": {
                        **orchestration_provenance,
                        "node_package_ref": node.package_ref.model_dump(mode="json"),
                        "attempt_id": attempt.get("attempt_id"),
                        "target": attempt.get("target"),
                        "slave_id": attempt.get("target"),
                    },
                }
            )
        if final_loaded and digest not in completed_digests:
            schema_errors.append(
                SchemaValidationError(
                    path="$",
                    keyword="lineage",
                    message="final output must reference a completed dynamic node",
                    expected="completed node result",
                    observed=final_ref.resource_id,
                )
            )
        if not schema_errors and contract is not None and contract.success_validator_ref is not None:
            validator_errors: list[dict[str, Any]] = []
            try:
                validator_program = await self.content_store.get(contract.success_validator_ref)
                validator_input = value if isinstance(value, dict) else {"value": value}
                validator_result = await default_registry.invoke_for(
                    "function",
                    ExecutionContract(kind="process:json_stdio", version="1"),
                    validator_input,
                    program=validator_program,
                )
                validator_payload = validator_result.value if isinstance(validator_result.value, dict) else {}
                if validator_payload.get("result") != "pass":
                    raw_errors = validator_payload.get("errors")
                    validator_errors = [item for item in raw_errors if isinstance(item, dict)] if isinstance(raw_errors, list) else []
            except Exception as exc:
                validator_errors = [{"path": "$", "keyword": "validator", "message": str(exc)[:512]}]
            if validator_errors:
                schema_errors.append(
                    SchemaValidationError(
                        path="$",
                        keyword="validator",
                        message="final output validator failed",
                        expected="validator pass",
                        observed=json.dumps(validator_errors, ensure_ascii=False),
                    )
                )
        requires_attestation = bool(
            not schema_errors
            and contract is not None
            and contract.success_validator_ref is None
            and contract.success_semantics is not None
        )
        if schema_errors:
            error_code = (
                "orchestration_lineage_mismatch"
                if any(item.keyword == "lineage" for item in schema_errors)
                else "output_schema_mismatch"
            )
            terminal_error = {"code": error_code, "errors": [item.model_dump(mode="json") for item in schema_errors]}
            _transition(record, "run_needs_decision", reason=terminal_error)
            record.outcome = {
                "disposition": "awaiting_decision",
                "decision": "repair",
                "terminal_state": "failed",
                "terminal_error": terminal_error,
                "resource_ref": None,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "provenance": orchestration_provenance,
            }
            record.events.append(
                {
                    "phase": "orchestration_failed",
                    "run_id": run_id,
                    "execution_id": record.execution_id,
                    "execution_epoch": record.execution_epoch,
                    "terminal_state": "failed",
                    "error": record.outcome["terminal_error"],
                    "provenance": orchestration_provenance,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._persist(record)
            await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
            return record
        if requires_attestation:
            _transition(record, "run_needs_decision")
            record.outcome = {
                "disposition": "awaiting_decision",
                "decision": "attestation",
                "terminal_state": "decision_required",
                "terminal_error": None,
                "resource_ref": final_ref.model_dump(mode="json"),
                "value": value,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "provenance": orchestration_provenance,
            }
            record.events.append(
                {
                    "phase": "orchestration_decision_required",
                    "run_id": run_id,
                    "execution_id": record.execution_id,
                    "execution_epoch": record.execution_epoch,
                    "terminal_state": "decision_required",
                    "resource_ref": final_ref.model_dump(mode="json"),
                    "provenance": orchestration_provenance,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._persist(record)
            await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
            return record
        _transition(record, "run_succeeded")
        record.outcome = {
            "terminal_state": "completed",
            "disposition": "completed",
            "decision": None,
            "resource_ref": final_ref.model_dump(mode="json"),
            "value": value,
            "execution_id": record.execution_id,
            "execution_epoch": record.execution_epoch,
            "lineage": lineage,
            "provenance": orchestration_provenance,
        }
        record.events.append(
            {
                "phase": "orchestration_completed",
                "run_id": run_id,
                "execution_id": record.execution_id,
                "execution_epoch": record.execution_epoch,
                "terminal_state": "completed",
                "resource_ref": final_ref.model_dump(mode="json"),
                "lineage": lineage,
                "provenance": orchestration_provenance,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self._persist(record)
        await self._settle_execution_request(record.run_id, execution_id=record.execution_id, outcome=record.outcome)
        return record


    async def _read_record(self, session, row):
        events = list((await session.scalars(select(RunEventRow.event).where(
            RunEventRow.run_id == row.run_id).order_by(RunEventRow.sequence))).all())
        return self._record_from_row(row, events)

    async def read_run_history(self, workspace_id, run_id, *, after=0, limit=100):
        await self.read_run_result(workspace_id, run_id)
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid_history_page")
        if self.sessions is None:
            events = self.runs[run_id].events[after:after + limit]
        else:
            async with self.sessions() as session:
                events = list((await session.scalars(select(RunEventRow.event).where(
                    RunEventRow.run_id == run_id, RunEventRow.sequence > after
                ).order_by(RunEventRow.sequence).limit(limit))).all())
        return {"events": events, "next_cursor": after + len(events)}

    async def read_run_view(self, workspace_id, run_id):
        if self.sessions is None:
            record = await self._assert_run_workspace(run_id, workspace_id)
        else:
            async with self.sessions() as session:
                row = (await session.scalars(select(RunRow).where(RunRow.run_id == run_id,
                    RunRow.closure_contract["workspace_id"].as_string() == workspace_id))).one_or_none()
                if row is None:
                    raise KeyError(run_id)
                record = self._record_from_row(row, [])
        value = run_record_payload(record, include_snapshots=False)
        value.update(closure_version=record.committed.version_id if record.committed else None,
            snapshot_digest=record.committed.snapshot_digest if record.committed else None)
        return value
