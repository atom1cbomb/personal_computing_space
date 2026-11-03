from __future__ import annotations
from loom_orchestration.naming import content_digest
from loom_orchestration.grip import Names
from loom_orchestration.mcue import McueDefinition, merge_attributes
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from copy import deepcopy
import json
import re
import hashlib
import hmac
from typing import Any
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import load_only
from loom_v2.contracts.types import ActionExport, CapabilityHealthReport, CapabilityPackage, CapabilityPackageActivation, CapabilityPackageVersion, ExecutionContract, IoContract, ResourceRef, TaskClosure, ManagedResource
from loom_v2.db.models import RunExecutionRequestRow, RunRow, RuntimeAgentRow, WorkspaceCapabilityPackageRow, CapabilityAuditEventRow, WorkspaceResourceRow, McueDefinitionRow
from loom_v2.db.migrations import apply_migrations
from loom_v2.contracts.agents import AgentLease, AgentRegistration, DriverThreadBinding
from loom_v2.contracts.messages import MessageReceipt
from loom_v2.db.session import make_session_factory
from loom_v2.content_store import ContentStore, canonical_json_bytes, put_typed_content
from loom_v2.digest import digest_bytes, digest_json
from loom_v2.contracts.io_schema import validate_schema
from loom_orchestration.contracts import CredentialProfile
from pydantic import TypeAdapter
from loom_v2.contracts.package_contracts import DRIVER_ORCHESTRATOR_KEY
from loom_v2.contracts.resource_effects import ResourceEffect, schema_path_exists, validate_action_descriptor
from loom_v2.contracts.errors import error
from loom_v2.contracts.refs import input_binding_for, action_name, require_content_ref, select_slave_agents, slave_supports_package
from loom_v2.settings import Settings
from loom_v2.repository_state import (RunSummary, PlanSummary, ExecutionRequest, PatchReceipt, RunCapabilities, RunRecord, execution_transaction, resource_transaction)

class ResourceRepository:
    """Deterministic state authority; SQL persistence is added behind this boundary."""

    def __init__(
        self,
        engine: AsyncEngine | None = None,
        content_store: ContentStore | None = None,
        *,
        settings: Settings | None = None,
        orchestrator_runtime_available: bool | None = None,
        action_descriptor_registry: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.idempotency: dict[str, PatchReceipt] = {}
        self.slave_agents: dict[str, dict[str, Any]] = {}
        self.slave_instances: dict[tuple[str, str], dict[str, Any]] = {}
        self.slave_capabilities: dict[str, dict[str, Any]] = {}
        self.workspace_packages: dict[tuple[str, str], CapabilityPackageVersion] = {}
        self.workspace_package_activations: dict[tuple[str, str], list[CapabilityPackageActivation]] = {}
        self.workspace_package_events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._run_capabilities: dict[str, RunCapabilities] = {}
        self.workspace_resources: dict[tuple[str, str], ManagedResource] = {}
        self.mcue_definitions: dict[tuple[str, str], dict[str, Any]] = {}
        self._mcue_content: dict[str, dict[str, Any]] = {}
        self._mcue_bootstrapped: set[str] = set()
        self._mcue_bootstrap_lock = asyncio.Lock()
        self._resource_lock = asyncio.Lock()
        self.engine = engine
        self.sessions = make_session_factory(engine) if engine is not None else None
        self.settings = settings or Settings()
        self.names = Names(self.settings.grip_authority, self.settings.workspace_id)
        from .service_routes import ServiceRoutes
        self.service_routes = ServiceRoutes(self.settings)
        if content_store is None:
            content_store = ContentStore.from_settings(self.settings)
        self.content_store = content_store
        # Optional authoritative action-descriptor registry.  Deployments
        # may populate it from their existing registry; content-addressed
        # descriptor refs are verified directly from ContentStore below.
        self.action_descriptor_registry = {
            key: deepcopy(value) for key, value in (action_descriptor_registry or {}).items()
        }
        # A deployed Observer deliberately has no Docker CLI/socket. Driver
        # readiness is responsible for its own runtime.
        self.orchestrator_runtime_available = orchestrator_runtime_available
        # Promotion mutates a RunRecord's package list.  The single Observer
        # process serializes concurrent retries so two requests cannot both
        # pass the existence check before either persists its derivative.
        self._promotion_lock = asyncio.Lock()
        self._dynamic_intent_locks: dict[str, asyncio.Lock] = {}
        self._activation_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        # Runtime agent state is kept in-memory when no database is configured
        # (the hermetic test profile) and mirrored to Observer PostgreSQL when
        # one is available. Lease ids are never persisted in plaintext.
        self.agents: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.driver_threads: dict[tuple[str, str], DriverThreadBinding] = {}
        self._driver_requests: dict[tuple[str, str], dict[str, Any]] = {}
        self._driver_request_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.agent_events: list[dict[str, Any]] = []
        self.message_receipts: dict[tuple[str, str], MessageReceipt] = {}
        self.message_receipt_events: list[dict[str, Any]] = []
        self._message_receipt_lock = asyncio.Lock()
        self.execution_requests: dict[tuple[str, str], ExecutionRequest] = {}
        self._execution_queue_lock = asyncio.Lock()
        self._execution_session = ContextVar("execution_session", default=None)
        self._execution_in_transaction = ContextVar("execution_in_transaction", default=False)
        self._execution_loaded_runs = ContextVar("execution_loaded_runs", default=None)
        self._execution_loaded_requests = ContextVar("execution_loaded_requests", default=None)

    @asynccontextmanager
    async def _execution_transaction(self, key: str | None = None):
        if self._execution_in_transaction.get():
            yield
            return
        from contextlib import AsyncExitStack
        async with AsyncExitStack() as stack:
            if self.sessions is None or self.engine.dialect.name != "postgresql":
                await stack.enter_async_context(self._execution_queue_lock)
            before = (deepcopy(self.runs), deepcopy(self.execution_requests), deepcopy(self._run_capabilities)) if self.sessions is None else None
            token = self._execution_in_transaction.set(True)
            runs_token = self._execution_loaded_runs.set(set())
            requests_token = self._execution_loaded_requests.set(set())
            session_token = None
            try:
                if self.sessions is not None:
                    session = await stack.enter_async_context(self.sessions())
                    await stack.enter_async_context(session.begin())
                    if key is not None and session.bind.dialect.name == "postgresql":
                        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                            {"key": "task:" + key})
                    session_token = self._execution_session.set(session)
                yield
                # Commit before exposing success; failed commits also invalidate cached state.
                await stack.aclose()
            except BaseException:
                if before is not None:
                    self.runs, self.execution_requests, self._run_capabilities = before
                for run_id in (self._execution_loaded_runs.get() or ()) if before is None else ():
                    self.runs.pop(run_id, None)
                    self._run_capabilities.pop(run_id, None)
                for request_key in (self._execution_loaded_requests.get() or ()) if before is None else ():
                    self.execution_requests.pop(request_key, None)
                raise
            finally:
                if session_token is not None:
                    self._execution_session.reset(session_token)
                self._execution_in_transaction.reset(token)
                self._execution_loaded_runs.reset(runs_token)
                self._execution_loaded_requests.reset(requests_token)


    @asynccontextmanager
    async def _execution_write_session(self):
        session = self._execution_session.get()
        if session is not None:
            yield session
            await session.flush()
        else:
            async with self.sessions() as session, session.begin():
                yield session

    def register_action_descriptor(self, resource_id: str, descriptor: Any, *, digest: str | None = None) -> str:
        """Register an immutable descriptor and return its canonical digest."""
        descriptor_payload = descriptor.model_dump(mode="json") if hasattr(descriptor, "model_dump") else deepcopy(descriptor)
        validate_action_descriptor(descriptor_payload)
        computed = digest_json(descriptor_payload, domain="loom/action-descriptor/v1")
        if digest is not None and digest.lower() != computed:
            raise ValueError("action_descriptor_digest_mismatch")
        self.action_descriptor_registry[(resource_id, computed)] = descriptor_payload
        return computed

    async def get_action_descriptor(self, ref: ResourceRef) -> dict[str, Any]:
        """Resolve a descriptor and validate it against the current authoring contract."""
        payload = await self.read_action_descriptor(ref)
        validate_action_descriptor(payload)
        return payload

    async def read_action_descriptor(self, ref: ResourceRef) -> dict[str, Any]:
        """Read immutable evidence with digest verification, including historical packages."""
        digest = (ref.digest or "").lower()
        if not digest:
            raise ValueError("action_descriptor_unavailable")
        if content_digest(ref.resource_id) is not None:
            content_ref = ResourceRef(
                resource_id=ref.resource_id,
                identity_criterion="content_digest",
            )
            raw = await self.content_store.get(content_ref)
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("action_descriptor_unavailable") from exc
            if digest_json(payload, domain="loom/action-descriptor/v1") != digest:
                raise ValueError("action_descriptor_digest_mismatch")
            if not isinstance(payload, dict):
                raise ValueError("action_descriptor_unavailable")
            return payload
        payload = self.action_descriptor_registry.get((ref.resource_id, digest))
        if payload is None:
            raise ValueError("action_descriptor_unavailable")
        payload = deepcopy(payload)
        if digest_json(payload, domain="loom/action-descriptor/v1") != digest:
            raise ValueError("action_descriptor_digest_mismatch")
        return payload

    async def public_package_payload(self, package: CapabilityPackageVersion) -> dict[str, Any]:
        """Return a package projection with its public action descriptors inline.

        Capability exports historically exposed only descriptor references.  A
        public agent has no access to the Observer's in-process symbolic
        descriptor registry, so the Observer data plane now includes the
        verified descriptor payload alongside each reference.
        """
        item = {**package.model_dump(mode="json"), "resource": package.version_ref}
        descriptors: list[dict[str, Any]] = []
        for export in package.action_exports:
            ref = export.action_descriptor_ref
            entry: dict[str, Any] = {"resource_ref": ref.model_dump(mode="json")}
            try:
                entry["descriptor"] = await self.read_action_descriptor(ref)
            except (FileNotFoundError, TypeError, ValueError) as exc:
                entry["error"] = str(exc) or "action_descriptor_unavailable"
            descriptors.append(entry)
        item["action_descriptors"] = descriptors
        return item

    async def _validate_action_descriptor_ref(self, ref: ResourceRef) -> None:
        await self.get_action_descriptor(ref)

    async def _validate_package_exports(self, package: CapabilityPackageVersion, *, publication: bool = False) -> None:
        if publication and "credential_profile" in (package.body or {}):
            raise ValueError("credential_profile_requires_activation")
        if package.how_to_use_ref is not None:
            stat = await self.content_store.stat(package.how_to_use_ref)
            if stat is None or not (stat.media_type.startswith("text/") or stat.media_type.endswith("+markdown")):
                raise ValueError("how_to_use_content_invalid")
            try:
                await self.content_store.get(package.how_to_use_ref)
            except (FileNotFoundError, TypeError, ValueError) as exc:
                raise ValueError("how_to_use_content_invalid") from exc
        authentication = (package.body or {}).get("authentication")
        if publication and authentication and ("interaction" not in authentication or "client" in authentication):
            raise ValueError("authentication_interaction_required")
        for action_export in package.action_exports:
            await self._validate_action_descriptor_ref(action_export.action_descriptor_ref)
            try:
                contract = IoContract.model_validate(await self._load_json_content(action_export.io_contract_ref))
                descriptor = await self.get_action_descriptor(action_export.action_descriptor_ref)
                for schema_ref in (contract.input_schema_ref, contract.output_schema_ref):
                    if schema_ref is not None:
                        validate_schema(await self._load_json_content(schema_ref))
                effects = descriptor.get("resource_effects") or []
                if effects:
                    if contract.output_schema_ref is None:
                        raise ValueError("resource_effect_output_schema_required")
                    output_schema = await self._load_json_content(contract.output_schema_ref)
                    for raw_effect in effects:
                        effect = ResourceEffect.model_validate(raw_effect)
                        if not schema_path_exists(output_schema, effect.identity_path):
                            raise ValueError("resource_effect_identity_path_unavailable")
                if contract.success_validator_ref is not None:
                    require_content_ref(contract.success_validator_ref)
                    await self.content_store.get(contract.success_validator_ref)
            except (FileNotFoundError, TypeError, ValueError) as exc:
                raise ValueError("io_contract_invalid") from exc
        from .resource_metadata import activation_config_schema, validate_resource_dependencies

        await validate_resource_dependencies(self, package)
        await activation_config_schema(self, package)


    @staticmethod
    def _lease_hash(lease_id: str) -> str:
        return hashlib.sha256(lease_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc_timestamp(value: Any) -> float | None:
        """Normalize lease timestamps from timezone-aware and SQLite values."""
        if value is None or not hasattr(value, "timestamp"):
            return None
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    async def register_agent(self, registration: AgentRegistration) -> AgentLease:
        """Replace only the same agent identity; independent Drivers coexist."""
        registration = AgentRegistration.model_validate(registration)
        if registration.resource_metadata is not None:
            metadata = registration.resource_metadata
            definition = await self.get_mcue_definition(registration.workspace_id, metadata.mcue_ref)
            if registration.role != "slave" or definition.mcue != "computing/slave":
                raise ValueError("agent_resource_mcue_invalid")
            definition.validate_attributes(metadata.attributes)
            registration.capabilities["resource_metadata"] = metadata.model_dump(mode="json")
        now = datetime.now(timezone.utc)
        lease_id = f"lease-{uuid4().hex}"
        key = (registration.workspace_id, registration.role, registration.agent_id, registration.instance_id)
        values = dict(workspace_id=registration.workspace_id, role=registration.role,
            agent_id=registration.agent_id, instance_id=registration.instance_id,
            endpoint_url=registration.endpoint_url.rstrip("/"), protocol_version=registration.protocol_version,
            capabilities=deepcopy(registration.capabilities), lease_id_hash=self._lease_hash(lease_id),
            lease_state="active", last_seen_at=now, created_at=now, updated_at=now)
        if self.sessions is None:
            previous = [v for k, v in self.agents.items() if k[:3] == key[:3]]
            epoch = max((v["epoch"] for v in previous), default=0) + 1
            for value in previous:
                value["lease_state"] = "expired"
            self.agents[key] = dict(values, epoch=epoch)
        else:
            async with self.sessions() as session, session.begin():
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": "agent:" + ":".join(key[:3])})
                rows = (await session.scalars(select(RuntimeAgentRow).where(
                    RuntimeAgentRow.workspace_id == key[0], RuntimeAgentRow.role == key[1],
                    RuntimeAgentRow.agent_id == key[2]).with_for_update())).all()
                epoch = max((r.epoch for r in rows), default=0) + 1
                for row in rows:
                    row.lease_state = "expired"
                await session.flush()
                row = next((r for r in rows if r.instance_id == key[3]), None)
                if row is None:
                    session.add(RuntimeAgentRow(**values, epoch=epoch))
                else:
                    for name, value in dict(values, epoch=epoch).items():
                        setattr(row, name, value)
        return AgentLease(agent_id=key[2], instance_id=key[3], workspace_id=key[0], lease_id=lease_id,
            epoch=epoch, heartbeat_interval_seconds=self.settings.agent_heartbeat_interval_seconds)


    async def _agent_record(self, workspace_id: str, role: str, agent_id: str, instance_id: str | None = None) -> dict[str, Any] | None:
        key = (workspace_id, role, agent_id)
        if self.sessions is None:
            matching = [
                item
                for candidate_key, item in self.agents.items()
                if candidate_key[:3] == key and (instance_id is None or item.get("instance_id") == instance_id)
            ]
            if not matching:
                return None
            matching.sort(key=lambda item: item.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return matching[0]
        async with self.sessions() as session:
            query = select(RuntimeAgentRow).where(RuntimeAgentRow.workspace_id == workspace_id, RuntimeAgentRow.role == role, RuntimeAgentRow.agent_id == agent_id)
            if instance_id is not None:
                query = query.where(RuntimeAgentRow.instance_id == instance_id)
            row = (await session.scalars(query.order_by(RuntimeAgentRow.updated_at.desc()))).first()
            if row is None:
                return None
            return {
                "workspace_id": row.workspace_id,
                "role": row.role,
                "agent_id": row.agent_id,
                "instance_id": row.instance_id,
                "endpoint_url": row.endpoint_url,
                "protocol_version": row.protocol_version,
                "capabilities": row.capabilities or {},
                "epoch": row.epoch,
                "lease_id_hash": row.lease_id_hash,
                "lease_state": row.lease_state,
                "last_seen_at": row.last_seen_at,
            }

    async def heartbeat_agent(self, agent_id: str, instance_id: str, lease_id: str, epoch: int, *, workspace_id: str | None = None, role: str = "driver") -> AgentLease:
        record = None
        if workspace_id is not None:
            await self.list_agents(workspace_id, role=role)
            record = await self._agent_record(workspace_id, role, agent_id, instance_id)
        else:
            candidates = [item for key, item in self.agents.items() if key[1] == role and key[2] == agent_id]
            if candidates:
                record = candidates[0]
                workspace_id = str(record["workspace_id"])
            elif self.sessions is not None:
                async with self.sessions() as session:
                    row = (await session.scalars(select(RuntimeAgentRow).where(RuntimeAgentRow.agent_id == agent_id, RuntimeAgentRow.role == role))).first()
                    if row is not None:
                        workspace_id = row.workspace_id
                        record = {
                            "workspace_id": row.workspace_id,
                            "role": row.role,
                            "agent_id": row.agent_id,
                            "instance_id": row.instance_id,
                            "endpoint_url": row.endpoint_url,
                            "protocol_version": row.protocol_version,
                            "capabilities": row.capabilities or {},
                            "epoch": row.epoch,
                            "lease_id_hash": row.lease_id_hash,
                            "lease_state": row.lease_state,
                        }
        if record is None or workspace_id is None or record.get("instance_id") != instance_id or record.get("lease_state") != "active" or int(record.get("epoch", 0)) != int(epoch) or not hmac.compare_digest(str(record.get("lease_id_hash", "")), self._lease_hash(lease_id)):
            raise ValueError("stale_driver_epoch" if role == "driver" else "stale_agent_lease")
        now = datetime.now(timezone.utc)
        if self.sessions is None:
            record["last_seen_at"] = now
            record["updated_at"] = now
        else:
            async with self.sessions() as session:
                row = (await session.scalars(select(RuntimeAgentRow).where(RuntimeAgentRow.workspace_id == workspace_id, RuntimeAgentRow.role == role, RuntimeAgentRow.agent_id == agent_id, RuntimeAgentRow.instance_id == instance_id))).first()
                if row is not None:
                    row.last_seen_at = now
                    row.updated_at = now
                    await session.commit()
        return AgentLease(agent_id=agent_id, instance_id=instance_id, workspace_id=workspace_id, lease_id=lease_id, epoch=int(epoch), heartbeat_interval_seconds=5.0)

    async def release_agent(self, agent_id: str, instance_id: str, lease_id: str, epoch: int, *, workspace_id: str | None = None, role: str = "driver") -> None:
        await self.heartbeat_agent(agent_id, instance_id, lease_id, epoch, workspace_id=workspace_id, role=role)
        if workspace_id is None:
            raise ValueError("agent_not_found")
        now = datetime.now(timezone.utc)
        if self.sessions is None:
            record = next(
                item
                for candidate_key, item in self.agents.items()
                if candidate_key[:3] == (workspace_id, role, agent_id) and item.get("instance_id") == instance_id
            )
            record["lease_state"] = "released"
            record["updated_at"] = now
        else:
            async with self.sessions() as session:
                row = (await session.scalars(select(RuntimeAgentRow).where(RuntimeAgentRow.workspace_id == workspace_id, RuntimeAgentRow.role == role, RuntimeAgentRow.agent_id == agent_id, RuntimeAgentRow.instance_id == instance_id))).first()
                if row is not None:
                    row.lease_state = "released"
                    row.updated_at = now
                    await session.commit()

    async def list_agents(self, workspace_id: str, role: str | None = None) -> list[dict[str, Any]]:
        lease_ttl = self.settings.agent_lease_ttl_seconds
        cutoff = datetime.now(timezone.utc).timestamp() - max(1.0, lease_ttl)
        if self.sessions is None:
            rows = []
            for key, item in self.agents.items():
                if key[0] != workspace_id or (role is not None and key[1] != role):
                    continue
                seen_timestamp = self._utc_timestamp(item.get("last_seen_at"))
                if item.get("lease_state") == "active" and seen_timestamp is not None and seen_timestamp < cutoff:
                    item["lease_state"] = "expired"
                rows.append(dict(item))
        else:
            async with self.sessions() as session:
                query = select(RuntimeAgentRow).where(RuntimeAgentRow.workspace_id == workspace_id)
                if role is not None:
                    query = query.where(RuntimeAgentRow.role == role)
                db_rows = (await session.scalars(query.order_by(RuntimeAgentRow.role, RuntimeAgentRow.agent_id))).all()
                rows = []
                expired = False
                for row in db_rows:
                    seen_timestamp = self._utc_timestamp(row.last_seen_at)
                    if row.lease_state == "active" and seen_timestamp is not None and seen_timestamp < cutoff:
                        row.lease_state = "expired"
                        row.updated_at = datetime.now(timezone.utc)
                        expired = True
                    rows.append({
                        "workspace_id": row.workspace_id,
                        "role": row.role,
                        "agent_id": row.agent_id,
                        "instance_id": row.instance_id,
                        "endpoint_url": row.endpoint_url,
                        "protocol_version": row.protocol_version,
                        "capabilities": row.capabilities or {},
                        "epoch": row.epoch,
                        "lease_state": row.lease_state,
                        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                    })
                if expired:
                    await session.commit()
        for row in rows:
            if hasattr(row.get("last_seen_at"), "isoformat"):
                row["last_seen_at"] = row["last_seen_at"].isoformat()
        return rows

    def _store_slave_snapshot(self, agents: list[dict[str, Any]]) -> None:
        self.slave_instances = {
            (str(agent.get("agent_id") or ""), str(agent.get("instance_id") or "")): dict(agent)
            for agent in agents
            if agent.get("agent_id")
        }
        self.slave_agents = select_slave_agents(agents)
        self.slave_capabilities = {}
        for slave_id, agent in self.slave_agents.items():
            if agent.get("lease_state") != "active":
                continue
            capabilities = dict(agent.get("capabilities") or {})
            base_actions = set(
                capabilities.get("base_actions", capabilities.get("actions", []))
            )
            self.slave_capabilities[slave_id] = {
                **capabilities,
                "base_actions": base_actions,
                "actions": set(base_actions),
            }

    def _project_ready_capabilities(self, slave_id: str | None = None) -> None:
        targets = [slave_id] if slave_id is not None else list(self.slave_capabilities)
        for target in targets:
            details = self.slave_capabilities.get(target)
            agent = self.slave_agents.get(target)
            if details is None or agent is None:
                continue
            capabilities = agent.get("capabilities") or {}
            base_actions = set(
                capabilities.get("base_actions", capabilities.get("actions", []))
            )
            projected = set(base_actions)
            for record in self._package_records():
                for activation in record.capability_activations:
                    if (
                        activation.target_slave != target
                        or activation.desired_state != "running"
                        or activation.activation_state != "ready"
                    ):
                        continue
                    # A reusable coordinate can be present in more than one
                    # run-bound record.  Resolve the manifest with the
                    # activation's digest assertion so a different package
                    # sharing the coordinate cannot hide this activation's
                    # exports from the projection.
                    package = self._find_package(
                        ResourceRef(
                            resource_id=activation.package_version_ref,
                            version_or_digest=activation.package_digest,
                        )
                    )
                    if package is None or package.package_digest != activation.package_digest:
                        continue
                    projected.update(
                        action_name(export.action_descriptor_ref.resource_id)
                        for export in package.action_exports
                    )
            for activation_list in self.workspace_package_activations.values():
                for activation in activation_list:
                    if (
                        activation.target_slave != target
                        or activation.desired_state != "running"
                        or activation.activation_state != "ready"
                    ):
                        continue
                    package = self._find_package(
                        ResourceRef(
                            resource_id=activation.package_version_ref,
                            version_or_digest=activation.package_digest,
                        )
                    )
                    if package is None or package.package_digest != activation.package_digest:
                        continue
                    projected.update(
                        action_name(export.action_descriptor_ref.resource_id)
                        for export in package.action_exports
                    )
            details["actions"] = projected

    async def refresh_slaves(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        agents = await self.list_agents(workspace_id or "workspace-default", role="slave")
        await self._load_run_capabilities()
        await self._load_workspace_packages()
        self._store_slave_snapshot(agents)
        self._project_ready_capabilities()
        return agents


    async def _resource_run_workspace(self, run_id: str) -> str:
        """Read only ownership metadata; resource checks never decode task history."""
        if self.sessions is None:
            record = self.runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return record.closure_contract.workspace_id if record.closure_contract else "workspace-default"
        query = select(RunRow.closure_contract["workspace_id"].as_string()).where(RunRow.run_id == run_id)
        session = self._execution_session.get()
        if session is not None:
            row = (await session.execute(query)).first()
        else:
            async with self.sessions() as session:
                row = (await session.execute(query)).first()
        if row is None:
            raise KeyError(run_id)
        return row[0] or "workspace-default"

    async def _assert_package_workspace(self, package_ref: str | ResourceRef, workspace_id: str, *, run_id: str | None = None) -> CapabilityPackageVersion:
        if run_id is not None and await self._resource_run_workspace(run_id) != workspace_id:
            raise ValueError("workspace_binding_mismatch")
        package = await self.get_capability_package(package_ref, run_id=run_id)
        if package.workspace_id is not None:
            package_workspace = package.workspace_id
        elif package.source_run_ref is not None:
            package_workspace = await self._resource_run_workspace(package.source_run_ref)
        else:
            raise ValueError("workspace_binding_mismatch")
        if package_workspace != workspace_id:
            raise ValueError("workspace_binding_mismatch")
        return package


    async def init_db(self) -> None:
        if self.engine is None:
            await self.ensure_mcue_definitions(self.settings.workspace_id)
            return
        async with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                await apply_migrations(connection, "observer")
            else:
                # SQLite is the intentionally ephemeral test backend.  It has
                # no production upgrade path, so metadata creation keeps the
                # persistence tests hermetic without becoming a second
                # PostgreSQL schema owner.
                from loom_v2.db.base import Base

                await connection.run_sync(Base.metadata.create_all)
        await self.ensure_mcue_definitions(self.settings.workspace_id)
        # A health report may have completed while the Observer process was
        # down. Reconcile persisted activation waits after the schema and
        # workspace projections are available so those Runs are claimable
        # again without a manual resume call.


    async def put_content(self, content: Any, *, media_type: str, name: str | None = None) -> ResourceRef:
        """Canonicalize and persist content through the single upload path."""
        return await put_typed_content(self.content_store, content, media_type=media_type, name=name)


    @staticmethod
    def _workspace_package_key(package: CapabilityPackageVersion) -> tuple[str, str]:
        if not package.workspace_id:
            raise ValueError("workspace_package_workspace_required")
        return package.workspace_id, package.version_ref

    def _package_records(self) -> list[RunCapabilities]:
        records = dict(self._run_capabilities)
        touched = self._execution_loaded_runs.get() or set()
        for key, record in self.runs.items():
            if self.sessions is None or key not in records or key in touched:
                records[key] = RunCapabilities(key,
                    record.closure_contract.workspace_id if record.closure_contract else None,
                    record.capability_packages, record.capability_activations)
        return list(records.values())

    async def _load_run_capabilities(self) -> None:
        """Refresh catalog projections without reading execution bodies or history."""
        if self.sessions is None:
            return
        async with self._execution_write_session() as session:
            rows = (await session.execute(select(
                RunRow.run_id, RunRow.closure_contract["workspace_id"].as_string(),
                RunRow.capability_packages, RunRow.capability_activations,
            ))).all()
        self._run_capabilities = {key: RunCapabilities(key, workspace,
            [CapabilityPackageVersion.model_validate(p) for p in packages or []],
            [CapabilityPackageActivation.model_validate(a) for a in activations or []])
            for key, workspace, packages, activations in rows}

    def _run_workspace(self, run_id: str) -> str | None:
        record = self.runs.get(run_id)
        if record is not None:
            return record.closure_contract.workspace_id if record.closure_contract else None
        projection = self._run_capabilities.get(run_id)
        return projection.workspace_id if projection else None

    async def _load_workspace_packages(self) -> None:
        if self.sessions is None:
            return
        async with self._execution_write_session() as session:
            rows = (await session.execute(select(
                WorkspaceCapabilityPackageRow.workspace_id,
                WorkspaceCapabilityPackageRow.package_ref,
                WorkspaceCapabilityPackageRow.package,
                WorkspaceCapabilityPackageRow.capability_activations,
            ))).all()
        for row in rows:
            package = CapabilityPackageVersion.model_validate(row.package)
            key = (row.workspace_id, row.package_ref)
            self.workspace_packages[key] = package
            self.workspace_package_activations[key] = [
                CapabilityPackageActivation.model_validate(item)
                for item in (row.capability_activations or [])
            ]

    async def read_package_events(self, package: CapabilityPackageVersion, *, after: int = 0,
                                  limit: int = 100) -> list[dict[str, Any]]:
        """Explicit audit read, bounded by an ordered cursor; runtime writes never call it."""
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("audit_page_invalid")
        key = self._workspace_package_key(package)
        if self.sessions is None:
            return deepcopy(self.workspace_package_events.get(key, [])[after:after + limit])
        async with self.sessions() as session:
            return list((await session.scalars(select(CapabilityAuditEventRow.event).where(
                CapabilityAuditEventRow.workspace_id == key[0], CapabilityAuditEventRow.package_ref == key[1],
                CapabilityAuditEventRow.sequence > after,
            ).order_by(CapabilityAuditEventRow.sequence).limit(limit))).all())

    async def _health_report_event(self, package: CapabilityPackageVersion, report_id: str) -> dict | None:
        key = self._workspace_package_key(package)
        if self.sessions is None:
            return next((e for e in self.workspace_package_events.get(key, [])
                         if e.get("phase") == "capability_health_report" and e.get("report_id") == report_id), None)
        async with self._execution_write_session() as session:
            return await session.scalar(select(CapabilityAuditEventRow.event).where(
                CapabilityAuditEventRow.workspace_id == key[0], CapabilityAuditEventRow.package_ref == key[1],
                CapabilityAuditEventRow.report_id == report_id,
            ))

    @asynccontextmanager
    async def _resource_transaction(self, workspace_id: str, key: str):
        """Serialize resource writes, including the first registration."""
        if self.sessions is None:
            async with self._resource_lock:
                yield None
            return
        async with self.sessions() as session, session.begin():
            if session.bind.dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"loom:resource:{workspace_id}:{key}"},
                )
            else:
                # SQLite has no row locks; acquire its write transaction before
                # reading the value to be modified.
                await session.execute(text("BEGIN IMMEDIATE"))
            yield session

    async def ensure_mcue_definitions(self, workspace_id: str) -> None:
        if workspace_id in self._mcue_bootstrapped:
            return
        async with self._mcue_bootstrap_lock:
            if workspace_id in self._mcue_bootstrapped:
                return
            for path in sorted((Path(__file__).parents[1] / "mcue_types").glob("*.json")):
                await self.publish_mcue_type(workspace_id, json.loads(path.read_text()))
            self._mcue_bootstrapped.add(workspace_id)

    async def publish_mcue_type(self, workspace_id: str, payload: dict[str, Any]) -> str:
        try:
            definition = McueDefinition.model_validate(payload)
        except ValueError as exc:
            raise ValueError("mcue_definition_invalid") from exc
        body = definition.model_dump(mode="json")
        names = Names(self.settings.grip_authority, workspace_id)
        target = names.mcue(definition.mcue, definition.version)
        ref = await self.put_content(
            body, media_type="application/json",
            name=f"mcue-types/{definition.mcue}/{definition.version}",
        )
        async with self._resource_transaction(workspace_id, target) as session:
            if session is None:
                previous = self.mcue_definitions.get((workspace_id, target))
                if previous is not None and canonical_json_bytes(previous) != canonical_json_bytes(body):
                    raise ValueError("mcue_version_conflict")
                self.mcue_definitions[(workspace_id, target)] = deepcopy(body)
            else:
                row = await session.get(McueDefinitionRow, (workspace_id, definition.mcue, definition.version))
                if row is not None:
                    if row.content_ref != ref.model_dump(mode="json"):
                        raise ValueError("mcue_version_conflict")
                else:
                    session.add(McueDefinitionRow(
                        workspace_id=workspace_id, mcue=definition.mcue, version=definition.version,
                        content_ref=ref.model_dump(mode="json"),
                    ))
        self._mcue_content[ref.resource_id] = body
        return target

    async def list_mcue_definitions(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        await self.ensure_mcue_definitions(workspace_id)
        if self.sessions is None:
            return {uri: deepcopy(body) for (scope, uri), body in self.mcue_definitions.items() if scope == workspace_id}
        async with self.sessions() as session:
            rows = (await session.scalars(select(McueDefinitionRow).where(McueDefinitionRow.workspace_id == workspace_id))).all()
        result = {}
        names = Names(self.settings.grip_authority, workspace_id)
        for row in rows:
            ref = ResourceRef.model_validate(row.content_ref)
            if ref.resource_id not in self._mcue_content:
                self._mcue_content[ref.resource_id] = await self._load_json_content(ref)
            result[names.mcue(row.mcue, row.version)] = deepcopy(self._mcue_content[ref.resource_id])
        return result

    async def get_mcue_definition(self, workspace_id: str, target: str) -> McueDefinition:
        definitions = await self.list_mcue_definitions(workspace_id)
        if target not in definitions:
            raise KeyError(target)
        return McueDefinition.model_validate(definitions[target])

    @staticmethod
    def _resource_from_row(row: WorkspaceResourceRow) -> ManagedResource:
        if "mcue_ref" not in row.resource:
            raise ValueError("resource_mcue_migration_required")
        return ManagedResource.model_validate({**row.resource, "attributes_revision": row.attributes_revision})

    async def _load_workspace_resources(self, workspace_id: str) -> None:
        if self.sessions is None:
            return
        async with self.sessions() as session:
            rows = (await session.scalars(select(WorkspaceResourceRow).where(WorkspaceResourceRow.workspace_id == workspace_id))).all()
        self.workspace_resources = {key: value for key, value in self.workspace_resources.items() if key[0] != workspace_id} | {
            (row.workspace_id, row.resource_id): self._resource_from_row(row) for row in rows
        }

    async def register_resource(
        self, workspace_id: str, payload: ManagedResource | dict[str, Any]
    ) -> ManagedResource:
        """Register an identity or update state without overwriting attributes."""
        resource = payload if isinstance(payload, ManagedResource) else ManagedResource.model_validate(payload)
        names = Names(self.settings.grip_authority, workspace_id)
        if not resource.resource_ref.resource_id.startswith(names.root + "/resources/"):
            raise ValueError("resource_namespace_invalid")
        definition = await self.get_mcue_definition(workspace_id, resource.mcue_ref)
        if resource.managed_by_ref is not None:
            if resource.managed_by_ref.resource_id == resource.resource_ref.resource_id:
                raise ValueError("resource_manager_self_reference")
            await self.get_resource(workspace_id, resource.managed_by_ref)
        key = (workspace_id, resource.resource_ref.resource_id)
        async with self._resource_transaction(*key) as session:
            if session is None:
                row, existing = None, self.workspace_resources.get(key)
            else:
                row = await session.get(WorkspaceResourceRow, key, with_for_update=True)
                existing = self._resource_from_row(row) if row else None
            if existing is not None:
                if "managed_by_ref" in resource.model_fields_set and existing.managed_by_ref != resource.managed_by_ref:
                    raise ValueError("resource_binding_conflict")
                if existing.mcue_ref != resource.mcue_ref:
                    raise ValueError("resource_mcue_conflict")
                if "attributes" in resource.model_fields_set and canonical_json_bytes(existing.attributes) != canonical_json_bytes(resource.attributes):
                    raise ValueError("resource_attributes_require_update")
                resource = existing.model_copy(deep=True)
                if "state" in (payload.model_fields_set if isinstance(payload, ManagedResource) else payload):
                    resource.state = payload.state if isinstance(payload, ManagedResource) else payload["state"]
            else:
                definition.validate_attributes(resource.attributes)
                resource = resource.model_copy(deep=True, update={"attributes_revision": 1})
            if session is None:
                self.workspace_resources[key] = resource.model_copy(deep=True)
            elif row is None:
                session.add(WorkspaceResourceRow(
                    workspace_id=workspace_id, resource_id=key[1],
                    resource=resource.model_dump(mode="json", exclude={"attributes_revision"}),
                    attributes_revision=resource.attributes_revision,
                ))
            else:
                row.resource = resource.model_dump(mode="json", exclude={"attributes_revision"})
        return resource.model_copy(deep=True)

    async def delete_resource(
        self, workspace_id: str, target: str, *, force: bool = False,
    ) -> None:
        """Remove one directory record once its entity is reported deleted."""
        key = (workspace_id, target)
        async with self._resource_transaction(*key) as session:
            if session is None:
                row, resource = None, self.workspace_resources.get(key)
            else:
                row = await session.get(WorkspaceResourceRow, key, with_for_update=True)
                resource = self._resource_from_row(row) if row else None
            if resource is None:
                raise KeyError(target)
            if not force and resource.state != "deleted":
                raise error(
                    "resource_not_deleted", resource=target, state=resource.state,
                    managed_by=(resource.managed_by_ref.resource_id if resource.managed_by_ref else None),
                )
            if session is None:
                self.workspace_resources.pop(key)
            else:
                await session.delete(row)
        # Invalidate only after the database transaction has committed.
        self.workspace_resources.pop(key, None)

    async def update_resource_attributes(
        self, workspace_id: str, target: str, *, expected_attributes_revision: int,
        patch: dict[str, Any], mcue_ref: str | None = None,
    ) -> ManagedResource:
        definitions = await self.list_mcue_definitions(workspace_id)
        key = (workspace_id, target)
        async with self._resource_transaction(*key) as session:
            if session is None:
                row, current = None, self.workspace_resources.get(key)
            else:
                row = await session.get(WorkspaceResourceRow, key, with_for_update=True)
                current = self._resource_from_row(row) if row else None
            if current is None:
                raise KeyError(target)
            if current.attributes_revision != expected_attributes_revision:
                raise ValueError("attributes_revision_conflict")
            old_definition = McueDefinition.model_validate(definitions[current.mcue_ref])
            definition = McueDefinition.model_validate(definitions[mcue_ref or current.mcue_ref])
            if definition.mcue != old_definition.mcue:
                raise ValueError("resource_mcue_conflict")
            attributes = merge_attributes(current.attributes, patch)
            definition.validate_attributes(attributes)
            changed = canonical_json_bytes(attributes) != canonical_json_bytes(current.attributes) or (mcue_ref or current.mcue_ref) != current.mcue_ref
            resource = current.model_copy(deep=True, update={
                "attributes": attributes, "mcue_ref": mcue_ref or current.mcue_ref,
                "attributes_revision": current.attributes_revision + int(changed),
            })
            if session is None:
                self.workspace_resources[key] = resource.model_copy(deep=True)
            elif changed:
                row.resource = resource.model_dump(mode="json", exclude={"attributes_revision"})
                row.attributes_revision = resource.attributes_revision
        return resource

    async def list_resources(self, workspace_id: str) -> list[ManagedResource]:
        await self._load_workspace_resources(workspace_id)
        return [
            resource.model_copy(deep=True)
            for (scope, _), resource in sorted(self.workspace_resources.items())
            if scope == workspace_id
        ]

    async def list_resource_activations(
        self,
        workspace_id: str,
        resource_ref: ResourceRef | str,
        *,
        package_ref: str | ResourceRef | None = None,
        instance_name: str | None = None,
        activation_state: str | None = None,
    ) -> list[CapabilityPackageActivation]:
        """Return only activations explicitly bound to one workspace resource.

        A package being available on the same Slave is not a resource binding;
        the target_resource_ref is the authoritative relationship.
        """
        await self.get_resource(workspace_id, resource_ref)
        resource_id = resource_ref.resource_id if isinstance(resource_ref, ResourceRef) else resource_ref
        package_id = None
        package_digest = None
        if package_ref is not None:
            normalized = package_ref if isinstance(package_ref, ResourceRef) else ResourceRef(resource_id=package_ref)
            package_id = normalized.resource_id
            package_digest = normalized.digest
        result: list[CapabilityPackageActivation] = []
        await self._load_run_capabilities()
        for record in self._package_records():
            contract_workspace = record.workspace_id or workspace_id
            if contract_workspace != workspace_id:
                continue
            result.extend(record.capability_activations)
        await self._load_workspace_packages()
        for activations in self.workspace_package_activations.values():
            result.extend(activations)
        filtered: list[CapabilityPackageActivation] = []
        seen: set[tuple[str, str, str, int]] = set()
        for activation in result:
            target = activation.target_resource_ref
            if target is None or target.resource_id != resource_id:
                continue
            if package_id is not None and activation.package_version_ref != package_id:
                continue
            if package_digest is not None and activation.package_digest.lower() != package_digest.lower():
                continue
            if instance_name is not None and activation.instance_name != instance_name:
                continue
            if activation_state is not None and activation.activation_state != activation_state:
                continue
            key = (activation.package_version_ref, activation.target_slave, activation.instance_name, activation.activation_revision)
            if key not in seen:
                seen.add(key)
                filtered.append(activation)
        return sorted(filtered, key=lambda item: (item.package_version_ref, item.instance_name, item.target_slave, item.activation_revision))

    async def get_resource(
        self, workspace_id: str, resource_ref: ResourceRef | str
    ) -> ManagedResource:
        await self._load_workspace_resources(workspace_id)
        resource_id = resource_ref.resource_id if isinstance(resource_ref, ResourceRef) else resource_ref
        resource = self.workspace_resources.get((workspace_id, resource_id))
        if resource is None:
            raise KeyError(resource_id)
        if isinstance(resource_ref, ResourceRef) and resource_ref.digest is not None:
            if resource.resource_ref.digest != resource_ref.digest:
                raise ValueError("resource_digest_mismatch")
        return resource.model_copy(deep=True)

    async def _persist_workspace_package(
        self, package: CapabilityPackageVersion, *,
        activations: list[CapabilityPackageActivation] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        """Commit a current-state projection and newly appended audit events atomically."""
        key = self._workspace_package_key(package)
        activation_snapshot = deepcopy(activations if activations is not None else self.workspace_package_activations.get(key, []))
        appended = deepcopy(events or [])
        values = {"workspace_id": key[0], "package_ref": key[1],
                  "package": package.model_dump(mode="json"),
                  "capability_activations": [item.model_dump(mode="json") for item in activation_snapshot]}
        if self.sessions is not None:
            async with self._execution_write_session() as session:
                row = await session.get(WorkspaceCapabilityPackageRow, key, options=[load_only(
                    WorkspaceCapabilityPackageRow.workspace_id, WorkspaceCapabilityPackageRow.package_ref)])
                if row is None:
                    session.add(WorkspaceCapabilityPackageRow(**values, events=[]))
                else:
                    for field_name, value in values.items():
                        setattr(row, field_name, value)
                if appended:
                    sequence = await session.scalar(select(func.max(CapabilityAuditEventRow.sequence)).where(
                        CapabilityAuditEventRow.workspace_id == key[0], CapabilityAuditEventRow.package_ref == key[1])) or 0
                    for offset, event in enumerate(appended, 1):
                        session.add(CapabilityAuditEventRow(workspace_id=key[0], package_ref=key[1],
                            sequence=sequence + offset, event=event,
                            report_id=event.get("report_id") if event.get("phase") == "capability_health_report" else None))
        else:
            self.workspace_package_events.setdefault(key, []).extend(appended)
        self.workspace_packages[key] = package
        self.workspace_package_activations[key] = activation_snapshot

    async def register_workspace_package(
        self, workspace_id: str, manifest: dict[str, Any]
    ) -> CapabilityPackageVersion:
        """Validate and publish a package supplied without a Run."""
        package = CapabilityPackageVersion(
            resource_authority=self.settings.grip_authority,
            package_type=str(manifest.get("package_type") or "function"),
            package_id=str(manifest["package_id"]),
            package_version=str(manifest["package_version"]),
            package_closure_version_ref=f"workspace-package-{uuid4().hex[:12]}",
            source_run_ref=None,
            source_closure_version_ref=None,
            workspace_id=workspace_id,
            scope="workspace_reusable",
            publication_state="published",
            provenance=[{"source": "user_upload", "workspace_id": workspace_id}],
            execution=ExecutionContract.model_validate(manifest.get("execution") or {}),
            action_exports=[
                ActionExport.model_validate(item)
                for item in manifest["action_exports"]
            ],
            how_to_use_ref=(ResourceRef.model_validate(manifest["how_to_use_ref"])
                            if manifest.get("how_to_use_ref") is not None else None),
            body=(dict(manifest.get("body")) if manifest.get("body") is not None else None),
            package_digest=str(manifest.get("package_digest") or ""),
        )
        await self._validate_package_exports(package, publication=True)
        await self._load_workspace_packages()
        key = self._workspace_package_key(package)
        existing = self.workspace_packages.get(key)
        if existing is not None:
            if existing.package_digest != package.package_digest:
                raise ValueError("capability_package_identity_conflict")
            return existing
        await self._persist_workspace_package(package)
        return package


    @staticmethod
    def _record_order_key(record: RunRecord) -> str:
        timestamps = [event.get("created_at") for event in record.events if isinstance(event.get("created_at"), str)]
        return max(timestamps, default="")

    @staticmethod
    def _conversation_status_for_state(state: str) -> str:
        return {
            "opened": "idle",
            "closed": "idle",
            "thinking": "thinking",
            "committed": "executing",
            "running": "executing",
            "awaiting_auth": "awaiting_auth",
            "completed": "completed",
            "awaiting_decision": "awaiting_decision",
            "cancelled": "interrupted",
            "failed": "failed",
        }.get(state, "idle")

    @staticmethod
    def _message_receipt_status(state: str) -> str:
        return {
            "accepted": "queued",
            "queued": "queued",
            "in_flight": "thinking",
            "retryable": "queued",
            "completed": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(state, "queued")

    @classmethod
    def _conversation_status(cls, records: list[RunRecord]) -> str:
        if not records:
            return "idle"
        latest = max(records, key=cls._record_order_key)
        return cls._conversation_status_for_state(latest.state)

    def _canonical_target_resource_id(self, resource_id: str) -> str:
        """Map only exact workspace Slave names to internal scheduling keys."""
        names = Names(self.settings.grip_authority, self.settings.workspace_id)
        for slave_id in self.slave_capabilities:
            if resource_id in {slave_id, names.name("slaves", slave_id)}:
                return slave_id
        return resource_id

    def _slave_supports_package(self, slave_id: str, package: CapabilityPackageVersion) -> bool:
        return slave_supports_package(
            slave_id,
            package,
            agents=self.slave_agents,
            capabilities=self.slave_capabilities,
        )

    def _slave_is_active(self, slave_id: str) -> bool:
        agent = self.slave_agents.get(slave_id)
        return agent is not None and agent.get("lease_state") == "active"

    @staticmethod
    def _package_ref(package: CapabilityPackageVersion) -> str:
        return package.version_ref

    @classmethod
    def _reusable_matches_candidate(
        cls,
        reusable: CapabilityPackageVersion,
        candidate: CapabilityPackageVersion,
    ) -> bool:
        if reusable.scope != "workspace_reusable" or reusable.publication_state != "published":
            return False
        if reusable.package_id != candidate.package_id:
            return False
        # New versions carry an explicit lineage entry.  The deterministic
        # version/source fallback also recognizes versions produced before
        # lineage matching was fixed.
        candidate_ref = cls._package_ref(candidate)
        if any(
            entry.get("derived_from") == candidate_ref
            and entry.get("derived_from_digest") == candidate.package_digest
            for entry in reusable.provenance
        ):
            return True
        return (
            reusable.package_version == f"{candidate.package_version}-reusable"
            and reusable.package_type == candidate.package_type
            and reusable.package_digest == candidate.package_digest
            and reusable.source_run_ref == candidate.source_run_ref
            and reusable.source_closure_version_ref == candidate.source_closure_version_ref
        )

    def _package_visible_to_run(self, package: CapabilityPackageVersion, run_id: str | None) -> bool:
        if run_id is None:
            return package.scope == "run_bound" or (
                package.scope == "workspace_reusable" and package.publication_state == "published")
        workspace = self._run_workspace(run_id)
        if run_id not in self.runs and run_id not in self._run_capabilities:
            return False
        if package.scope == "run_bound":
            return package.source_run_ref == run_id
        if package.scope != "workspace_reusable" or package.publication_state != "published":
            return False
        source_workspace = package.workspace_id or (
            self._run_workspace(package.source_run_ref) if package.source_run_ref else None)
        return workspace is not None and source_workspace == workspace

    def _find_package(self, package_ref: str | ResourceRef, *, run_id: str | None = None) -> CapabilityPackageVersion | None:
        resource_id = package_ref.resource_id if isinstance(package_ref, ResourceRef) else package_ref
        digest = package_ref.digest if isinstance(package_ref, ResourceRef) and package_ref.digest else None
        candidates: list[CapabilityPackageVersion] = []
        for record in self._package_records():
            for package in record.capability_packages:
                if not self._package_visible_to_run(package, run_id):
                    continue
                if resource_id != package.version_ref:
                    continue
                if digest is not None and digest != package.package_digest.lower():
                    continue
                candidates.append(package)
        for package in self.workspace_packages.values():
            if not self._package_visible_to_run(package, run_id):
                continue
            if resource_id != package.version_ref:
                continue
            if digest is not None and digest != package.package_digest.lower():
                continue
            candidates.append(package)
        if not candidates:
            return None
        # Prefer a published reusable version when resolving without a run
        # context, then keep selection deterministic across persisted runs.
        candidates.sort(
            key=lambda item: (
                item.scope != "workspace_reusable",
                item.package_id,
                item.package_version,
                item.source_run_ref or "",
            )
        )
        return candidates[0]

    async def list_capability_packages(self, *, run_id: str | None = None, include_abandoned: bool = False, workspace_id: str | None = None) -> list[CapabilityPackageVersion]:
        await self._load_workspace_packages()
        await self._load_run_capabilities()
        records = [r for r in self._package_records() if run_id is None or r.run_id == run_id]
        if run_id is not None and not records:
            raise KeyError(run_id)
        packages = [package for record in records for package in record.capability_packages]
        if run_id is not None:
            run = records[0]
            resolved_workspace = (
                run.workspace_id
                if run.workspace_id is not None
                else workspace_id
            )
            packages.extend(
                package
                for package in self.workspace_packages.values()
                if (resolved_workspace is None or package.workspace_id == resolved_workspace)
                and self._package_visible_to_run(package, run_id)
            )
        else:
            packages.extend(
                package
                for package in self.workspace_packages.values()
                if workspace_id is None or package.workspace_id == workspace_id
            )
        if not include_abandoned:
            packages = [package for package in packages if package.publication_state != "abandoned"]
        return packages

    async def list_published_package_states(
        self, workspace_id: str, *, package_ref: str | None = None,
    ) -> list[tuple[CapabilityPackageVersion, list[CapabilityPackageActivation]]]:
        """Request-local directory projection; never reload state per package.

        Published versions can live in a Run or in the workspace package table.
        Read only package/activation columns, not the execution/event histories.
        """
        def visible(package):
            return (package.workspace_id == workspace_id and package.publication_state == "published"
                    and (package_ref is None or package.version_ref == package_ref))

        if self.sessions is None:
            packages = [p for record in self.runs.values() for p in record.capability_packages if visible(p)]
            packages.extend(p for (scope, _), p in self.workspace_packages.items() if scope == workspace_id and visible(p))
            return [(p.model_copy(deep=True), deepcopy(self.workspace_package_activations.get(
                (workspace_id, p.version_ref), [],
            ))) for p in packages]

        query = select(WorkspaceCapabilityPackageRow.package, WorkspaceCapabilityPackageRow.capability_activations).where(
            WorkspaceCapabilityPackageRow.workspace_id == workspace_id,
        )
        if package_ref is not None:
            query = query.where(WorkspaceCapabilityPackageRow.package_ref == package_ref)
        async with self.sessions() as session:
            run_packages = (await session.scalars(select(RunRow.capability_packages))).all()
            rows = (await session.execute(query)).all()
        activations = {}
        packages = []
        for values in run_packages:
            for value in values or []:
                if value.get("workspace_id") == workspace_id and value.get("publication_state") == "published":
                    package = CapabilityPackageVersion.model_validate(value)
                    if visible(package):
                        packages.append(package)
        for value, states in rows:
            package = CapabilityPackageVersion.model_validate(value)
            activations[package.version_ref] = [CapabilityPackageActivation.model_validate(a) for a in states or []]
            if visible(package):
                packages.append(package)
        return [(package, activations.get(package.version_ref, [])) for package in packages]

    async def get_capability_package(self, package_ref: str | ResourceRef, *, run_id: str | None = None) -> CapabilityPackageVersion:
        await self._load_workspace_packages()
        if run_id is not None:
            await self._load_run_capabilities()
            if run_id not in self._run_capabilities and run_id not in self.runs:
                raise KeyError(run_id)
        package = self._find_package(package_ref, run_id=run_id)
        if package is not None and package.workspace_id is not None:
            return package
        if self.sessions is not None:
            await self._load_run_capabilities()
        package = self._find_package(package_ref, run_id=run_id)
        if package is None:
            raise KeyError(package_ref)
        return package

    async def get_capability_package_aggregate(self, package_ref: str) -> CapabilityPackage:
        version = await self.get_capability_package(package_ref)
        await self._load_run_capabilities()
        records = self._package_records()
        key = (version.workspace_id, version.version_ref) if version.workspace_id else None
        activations = [activation for record in records for activation in record.capability_activations if activation.package_version_ref == version.version_ref]
        if key is not None:
            activations.extend(self.workspace_package_activations.get(key, []))
        versions = [item for record in records for item in record.capability_packages if item.package_id == version.package_id]
        versions.extend(
            item
            for item in self.workspace_packages.values()
            if item.package_id == version.package_id
        )
        return CapabilityPackage(package_id=version.package_id, versions=versions, activations=activations)

    async def list_package_activations(
        self, package: CapabilityPackageVersion, *, refresh: bool = True
    ) -> list[CapabilityPackageActivation]:
        if refresh:
            await self._load_workspace_packages()
            await self._load_run_capabilities()
        if package.workspace_id is not None:
            return list(
                self.workspace_package_activations.get(
                    self._workspace_package_key(package), []
                )
            )
        return [
            activation
            for record in self._package_records()
            for activation in record.capability_activations
            if activation.package_version_ref == package.version_ref
        ]

    @execution_transaction
    async def abandon_capability_package(self, package_ref: str, *, idempotency_key: str | None = None) -> CapabilityPackageVersion:
        package = await self.get_capability_package(package_ref)
        if package.publication_state == "abandoned":
            return package
        package.publication_state = "abandoned"
        if package.workspace_id is not None:
            events = []
            events.append(
                {
                    "phase": "capability_package_abandoned",
                    "package_ref": self._package_ref(package),
                    "idempotency_key": idempotency_key,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._persist_workspace_package(package, events=events)
            return package
        for projection in self._package_records():
            if any(item.package_id == package.package_id and item.package_version == package.package_version for item in projection.capability_packages):
                record = await self._load(projection.run_id)
                next(p for p in record.capability_packages if p.version_ref == package.version_ref).publication_state = "abandoned"
                record.events.append({"phase": "capability_package_abandoned", "package_ref": self._package_ref(package), "idempotency_key": idempotency_key, "created_at": datetime.now(timezone.utc).isoformat()})
                await self._persist(record)
                break
        return package

    async def promote_capability_package(self, package_ref: str | ResourceRef, *, idempotency_key: str | None = None, approved_digest: str | None = None) -> CapabilityPackageVersion:
        async with self._promotion_lock:
            # Resolve the input first.  A request normally carries the
            # candidate ref (for example ``.../v1``), while the derived
            # published version has a different identity (``.../v1-reusable``).
            # Idempotency therefore has to follow the candidate -> reusable
            # lineage, not compare the request string with the reusable ref.
            candidate = await self.get_capability_package(package_ref)
            if candidate.publication_state == "published" and candidate.scope == "workspace_reusable":
                return candidate

            candidate_ref = self._package_ref(candidate)
            reusable_version = f"{candidate.package_version}-reusable"
            for record in self._package_records():
                reusable = next(
                    (
                        item
                        for item in record.capability_packages
                        if self._reusable_matches_candidate(item, candidate)
                    ),
                    None,
                )
                if reusable is not None:
                    return reusable

            if candidate.publication_state == "abandoned":
                raise ValueError("capability_package_abandoned")
            if approved_digest and approved_digest != candidate.package_digest:
                raise ValueError("promotion_digest_mismatch")
            if (
                candidate.package_type,
                candidate.execution.kind,
                candidate.execution.version,
            ) == DRIVER_ORCHESTRATOR_KEY:
                if not bool(candidate.body.get("semantic_closed", True)):
                    raise ValueError("package_promotion_denied:semantic_not_closed")
                if (
                    bool(candidate.body.get("captures_run_state", False))
                    or candidate.body.get("captured_secret_refs")
                    or candidate.body.get("captured_path_refs")
                ):
                    raise ValueError("package_captures_run_state")
            await self._validate_package_exports(candidate, publication=True)
            source_record = next(
                (
                    record
                    for record in self._package_records()
                    if any(
                        item.package_id == candidate.package_id
                        and item.package_version == candidate.package_version
                        and item.package_digest == candidate.package_digest
                        for item in record.capability_packages
                    )
                ),
                None,
            )
            if source_record is None:
                raise KeyError(package_ref)
            source_record = await self._load(source_record.run_id)
            if source_record.state not in {"completed", "closed", "failed", "cancelled"}:
                raise ValueError("run_not_terminal")
            reusable_payload = candidate.model_dump(mode="json")
            reusable_payload.update({"package_version": reusable_version, "scope": "workspace_reusable", "publication_state": "published", "provenance": [*candidate.provenance, {"derived_from": candidate_ref, "derived_from_digest": candidate.package_digest, "idempotency_key": idempotency_key}], "package_digest": ""})
            reusable = CapabilityPackageVersion.model_validate(reusable_payload)
            for record in self._package_records():
                conflicting = next(
                    (
                        item
                        for item in record.capability_packages
                        if item.package_id == reusable.package_id
                        and item.package_version == reusable.package_version
                    ),
                    None,
                )
                if conflicting is not None:
                    if conflicting.package_digest == reusable.package_digest:
                        return conflicting
                    raise ValueError("capability_package_identity_conflict")
            source_record.capability_packages.append(reusable)
            source_record.events.append({"phase": "capability_package_promoted", "package_ref": self._package_ref(reusable), "derived_from": candidate_ref, "derived_from_digest": candidate.package_digest, "idempotency_key": idempotency_key, "created_at": datetime.now(timezone.utc).isoformat()})
            await self._persist(source_record)
            return reusable

    async def _activation_record(
        self, package: CapabilityPackageVersion
    ) -> RunRecord:
        await self._load_run_capabilities()
        records = self._package_records()
        record = next(
            (
                item
                for item in records
                if any(
                    candidate.version_ref == package.version_ref
                    and candidate.package_digest == package.package_digest
                    for candidate in item.capability_packages
                )
            ),
            None,
        )
        if record is None:
            raise KeyError(package.version_ref)
        return await self._load(record.run_id)

    @resource_transaction
    async def set_capability_desired(
        self,
        package_ref: str | ResourceRef,
        target_slave: str,
        *,
        desired_state: str,
        idempotency_key: str,
        run_id: str | None = None,
        workspace_id: str | None = None,
        activation_closure_version_ref: str = "",
        compute_binding_ref: str = "",
        rebuild: bool = False,
        instance_name: str = "default",
        target_resource_ref: ResourceRef | None = None,
        instance_config: dict[str, Any] | None = None,
        credential_profile: str | None = None,
        expected_revision: int | None = None,
        dependencies: dict | None = None,
        configure: bool = False,
    ) -> CapabilityPackageActivation:
        """Atomically assign the next desired-state revision for one activation."""
        from loom_v2.contracts.types import ActivationDependency
        if dependencies is not None:
            if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key) for key in dependencies):
                raise ValueError("activation_dependency_name_invalid")
            dependencies = {key: ActivationDependency.model_validate(value) for key, value in dependencies.items()}
        if credential_profile is not None:
            credential_profile = TypeAdapter(CredentialProfile).validate_python(credential_profile)
        if desired_state not in {"running", "stopped"}:
            raise ValueError("activation_desired_state_invalid")
        if not target_slave:
            raise ValueError("target_slave_required")
        if not idempotency_key:
            raise ValueError("idempotency_key_required")
        package = await self.get_capability_package(package_ref, run_id=run_id)
        if package.publication_state == "abandoned":
            raise ValueError("capability_package_abandoned")
        if package.is_interface:
            raise ValueError("capability_package_not_executable")
        record: RunRecord | None = None
        if package.workspace_id is not None:
            package_key = self._workspace_package_key(package)
            activation_list = self.workspace_package_activations.setdefault(package_key, [])
            event_list = []
            record_workspace = package.workspace_id
        else:
            record = await self._activation_record(package)
            activation_list = record.capability_activations
            event_list = record.events
            record_workspace = (
                record.closure_contract.workspace_id
                if record.closure_contract is not None
                else "workspace-default"
            )
        if target_resource_ref is not None:
            # The activation selects the access package. Its explicit target
            # binds it to a stable domain resource in the same workspace.
            await self.get_resource(record_workspace, target_resource_ref)
        if workspace_id is not None and workspace_id != record_workspace:
            raise ValueError("workspace_binding_mismatch")
        if desired_state == "running":
            self.service_routes.require(package, target_slave, instance_name)
        lock_key = (record_workspace, package.version_ref, "*", "*") if package.workspace_id else (record_workspace, package.version_ref, target_slave, instance_name)
        lock = self._activation_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            session = self._execution_session.get()
            if session is not None and session.bind.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "activation:" + ":".join(lock_key[:2])})
            if record is None:
                await self._load_workspace_packages()
                activation_list = self.workspace_package_activations.setdefault(package_key, [])
                event_list = []
            else:
                record = await self._activation_record(package)
                activation_list = record.capability_activations
                event_list = record.events
            current = next(
                (
                    item
                    for item in activation_list
                    if item.package_version_ref == package.version_ref
                    and item.target_slave == target_slave
                    and item.instance_name == instance_name
                ),
                None,
            )
            if configure and (current is None or current.desired_state != "running" or expected_revision is None):
                raise ValueError("activation_revision_conflict")
            if current is not None and current.package_digest != package.package_digest:
                raise ValueError("capability_package_identity_conflict")
            if desired_state == "running":
                from .resource_metadata import validate_activation_config, validate_resource_dependencies

                await validate_resource_dependencies(self, package)
                await validate_activation_config(
                    self, package,
                    instance_config if instance_config is not None else (current.instance_config if current else {}),
                )
            if current is not None:
                requested_target = target_resource_ref if target_resource_ref is not None else current.target_resource_ref
                requested_config = dict(current.instance_config if instance_config is None else instance_config)
                requested_profile = current.credential_profile if credential_profile is None else credential_profile
                if (requested_target != current.target_resource_ref or (not configure and requested_config != current.instance_config)
                        or requested_profile != current.credential_profile
                        or (not configure and dependencies is not None and dependencies != current.dependencies)):
                    raise ValueError("activation_instance_binding_conflict")
            if current is not None and current.last_idempotency_key == idempotency_key:
                if (current.desired_state != desired_state
                        or (instance_config is not None and instance_config != current.instance_config)
                        or (dependencies is not None and dependencies != current.dependencies)):
                    raise ValueError("idempotency_key_reused")
                return current
            if expected_revision is not None and (current is None or current.activation_revision != expected_revision):
                raise ValueError("activation_revision_conflict")
            if (
                current is not None
                and current.desired_state == desired_state
                and not rebuild and not configure
                # A failed transition must be retryable with a new
                # activation revision.  Keeping the old revision here leaves
                # the Slave fenced on its failed projection and makes a
                # subsequent promote/deactivate permanently return
                # ``activation_revision_conflict``.
                and current.activation_state != "failed"
            ):
                return current

            revision = 1 if current is None else current.activation_revision + 1
            if current is not None:
                # Keep the failed/stale attempt in the event log for audit,
                # while replacing the live projection with a new fenced
                # revision. Retrying a coordinate must not require deleting
                # its history first.
                event_list.append(
                    {
                        "phase": "capability_activation_superseded",
                        "package_ref": package.version_ref,
                        "package_digest": package.package_digest,
                        "target_slave": target_slave,
                        "instance_name": instance_name,
                        "old_activation_revision": current.activation_revision,
                        "old_activation_state": current.activation_state,
                        "new_activation_revision": revision,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            actual_state = (
                "provisioning"
                if desired_state == "running"
                else current.activation_state if current is not None else "stopped"
            )
            activation = CapabilityPackageActivation(
                package_version_ref=package.version_ref,
                package_digest=package.package_digest,
                target_slave=target_slave,
                activation_closure_version_ref=(
                    activation_closure_version_ref
                    or (
                        current.activation_closure_version_ref
                        if current is not None
                        else package.package_closure_version_ref
                    )
                ),
                compute_binding_ref=(
                    compute_binding_ref
                    or (current.compute_binding_ref if current is not None else "")
                ),
                desired_state=desired_state,
                activation_revision=revision,
                last_idempotency_key=idempotency_key,
                # Evidence belongs to one activation revision. Never carry a
                # failed attempt's container identity into its retry.
                evidence_refs=[],
                activation_state=actual_state,
                runtime_profile={},
                instance_name=instance_name,
                target_resource_ref=target_resource_ref if target_resource_ref is not None else (current.target_resource_ref if current is not None else None),
                instance_config=dict(instance_config if instance_config is not None else (current.instance_config if current is not None else {})),
                credential_profile=credential_profile if credential_profile is not None else (current.credential_profile if current else None),
                dependencies=dependencies if dependencies is not None else (current.dependencies if current else {}),
                applied_revision=current.applied_revision if current else None,
            )
            activation_list[:] = [
                item
                for item in activation_list
                if not (
                    item.package_version_ref == package.version_ref
                    and item.target_slave == target_slave
                    and item.instance_name == instance_name
                )
            ]
            activation_list.append(activation)
            event_list.append(
                {
                    "phase": "capability_desired_state_changed",
                    "package_ref": package.version_ref,
                    "package_digest": package.package_digest,
                    "target_slave": target_slave,
                    "desired_state": desired_state,
                    "activation_revision": revision,
                    "idempotency_key": idempotency_key,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if record is not None:
                await self._persist(record)
            else:
                await self._persist_workspace_package(package, activations=activation_list, events=event_list)
            self._project_ready_capabilities(target_slave)
            return activation

    @resource_transaction
    async def record_capability_health(
        self, report: CapabilityHealthReport, *, run_id: str | None = None,
        authentication_restart: bool = False,
    ) -> CapabilityPackageActivation:
        if not isinstance(report, CapabilityHealthReport):
            report = CapabilityHealthReport.model_validate(report)
        package = await self.get_capability_package(
            report.package_version_ref, run_id=run_id
        )
        if package.workspace_id is not None:
            workspace = package.workspace_id
        else:
            record = await self._activation_record(package)
            workspace = (
                record.closure_contract.workspace_id
                if record.closure_contract is not None
                else "workspace-default"
            )
        lock_key = (workspace, package.version_ref, "*", "*") if package.workspace_id else (workspace, package.version_ref, report.target_slave, report.instance_name)
        lock = self._activation_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            session = self._execution_session.get()
            if session is not None and session.bind.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "activation:" + ":".join(lock_key[:2])})
            return await self._record_capability_health_locked(
                report, package=package, authentication_restart=authentication_restart)

    async def _record_capability_health_locked(
        self, report: CapabilityHealthReport, *, package: CapabilityPackageVersion,
        authentication_restart: bool = False,
    ) -> CapabilityPackageActivation:
        """Accept an actual-state report only for the current desired revision."""
        if package.package_digest != report.package_digest:
            raise ValueError("capability_package_digest_mismatch")
        record: RunRecord | None = None
        if package.workspace_id is not None:
            package_key = self._workspace_package_key(package)
            # Refresh the current revision after acquiring the activation lock.
            # Package lookup may have raced a desired-state update.
            await self._load_workspace_packages()
            activation_list = self.workspace_package_activations.setdefault(package_key, [])
            event_list = []
        else:
            record = await self._activation_record(package)
            activation_list = record.capability_activations
            event_list = record.events
        current = next(
            (
                item
                for item in activation_list
                if item.package_version_ref == package.version_ref
                and item.target_slave == report.target_slave
                and item.instance_name == report.instance_name
            ),
            None,
        )
        if current is None:
            raise ValueError("activation_desired_state_missing")
        if report.activation_revision < current.activation_revision:
            raise ValueError("stale_activation_revision")
        if report.activation_revision > current.activation_revision:
            raise ValueError("future_activation_revision")

        previous_report = (await self._health_report_event(package, report.report_id)
                           if package.workspace_id is not None else next(
                               (event for event in event_list if event.get("phase") == "capability_health_report"
                                and event.get("report_id") == report.report_id), None))
        if previous_report is not None:
            if (
                previous_report.get("package_digest") == report.package_digest
                and previous_report.get("target_slave") == report.target_slave
                and previous_report.get("instance_name") == report.instance_name
                and previous_report.get("activation_revision")
                == report.activation_revision
                and previous_report.get("activation_state")
                == report.activation_state
                and previous_report.get("evidence_refs") == report.evidence_refs
                and previous_report.get("details") == report.details
            ):
                return current
            raise ValueError("health_report_id_reused")

        allowed_transitions = {
            "not_installed": {"pending_auth", "ready", "failed", "stopped"},
            "provisioning": {"pending_auth", "ready", "degraded", "failed", "stopped"},
            "pending_auth": {"pending_auth", "ready", "degraded", "failed", "stopped"},
            "ready": {"pending_auth", "ready", "degraded", "failed", "stopped"},
            "degraded": {"pending_auth", "ready", "degraded", "failed", "stopped"},
            "failed": {"failed", "stopped"},
            "stopped": {"stopped"},
            "lost": {"pending_auth", "ready", "degraded", "failed", "stopped"},
        }
        if authentication_restart:
            auth = (report.details or {}).get("authentication", {})
            expected_state = {"authenticated": "ready", "auth_required": "pending_auth", "auth_failed": "degraded"}.get(auth.get("status"))
            if (current.desired_state != "running" or not (package.body or {}).get("authentication")
                    or report.activation_state != expected_state):
                raise ValueError("authentication_restart_report_invalid")
            allowed_transitions["failed"] = {"pending_auth", "ready", "degraded"}
        if report.activation_state not in allowed_transitions[current.activation_state]:
            raise ValueError("activation_state_transition_invalid")
        runtime_profile = (
            dict(report.details.get("runtime_profile") or {})
            if isinstance(report.details, dict)
            else {}
        )
        activation = current
        if (
            report.activation_state != current.activation_state
            or report.evidence_refs != current.evidence_refs
            or runtime_profile
        ):
            activation = current.model_copy(
                update={
                    "activation_state": report.activation_state,
                    "applied_revision": report.activation_revision if report.activation_state == "ready" else current.applied_revision,
                    "evidence_refs": list(report.evidence_refs),
                    "runtime_profile": runtime_profile or current.runtime_profile,
                }
            )
            activation_list[:] = [
                item
                for item in activation_list
                if not (
                    item.package_version_ref == package.version_ref
                and item.target_slave == report.target_slave
                and item.instance_name == report.instance_name
                )
            ]
            activation_list.append(activation)
        event_list.append(
            {
                "phase": "capability_health_report",
                "report_id": report.report_id,
                "package_ref": package.version_ref,
                "package_digest": package.package_digest,
                    "target_slave": report.target_slave,
                    "instance_name": report.instance_name,
                "desired_state": activation.desired_state,
                "activation_revision": report.activation_revision,
                "activation_state": report.activation_state,
                "evidence_refs": report.evidence_refs,
                "details": report.details,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if record is not None:
            await self._persist(record)
        else:
            await self._persist_workspace_package(package, activations=activation_list, events=event_list)
        self._project_ready_capabilities(report.target_slave)
        return activation

    @classmethod
    def _input_digest_for(cls, snapshot: TaskClosure) -> str | None:
        action_ref = snapshot.program.action_ref or snapshot.compute.action_ref
        binding = input_binding_for(snapshot, action_ref)
        return binding.input_ref.digest if binding is not None else None

    @staticmethod
    def _result_digest(value: Any) -> str:
        return digest_bytes(canonical_json_bytes(value))


    @classmethod
    def _collect_content_ref_digests(cls, value: Any, output: set[str]) -> None:
        if isinstance(value, dict):
            if "resource_id" in value:
                try:
                    ref = ResourceRef.model_validate(value)
                    require_content_ref(ref)
                    output.add((ref.digest or "").lower())
                except (TypeError, ValueError):
                    pass
            for item in value.values():
                cls._collect_content_ref_digests(item, output)
        elif isinstance(value, list):
            for item in value:
                cls._collect_content_ref_digests(item, output)

    async def _load_json_content(self, ref: ResourceRef) -> Any:
        require_content_ref(ref)
        raw = await self.content_store.get(ref)
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json") from exc


    async def set_slave_capabilities(self, slave_id: str, actions: set[str]) -> None:
        details = self.slave_capabilities.setdefault(slave_id, {})
        details["base_actions"] = set(actions)
        details["actions"] = set(actions)


    async def list_run_summaries(self, workspace_id: str, *, run_id: str | None = None) -> list[RunSummary]:
        """Read directory state without loading closure bodies, events or outcomes."""
        if self.sessions is None:
            return [RunSummary(
                record.run_id, record.state,
                record.committed.version_id if record.committed else None,
                record.committed.snapshot_digest if record.committed else None,
                record.execution_id, record.execution_epoch,
            ) for record in sorted(self.runs.values(), key=lambda item: item.run_id)
                if record.closure_contract is not None
                and record.closure_contract.workspace_id == workspace_id
                and (run_id is None or record.run_id == run_id)]
        query = select(
            RunRow.run_id, RunRow.state,
            RunRow.committed["version_id"].as_string(),
            RunRow.committed["snapshot_digest"].as_string(),
            RunRow.execution_id, RunRow.execution_epoch,
        ).where(
            RunRow.closure_contract["workspace_id"].as_string() == workspace_id,
        ).order_by(RunRow.run_id)
        if run_id is not None:
            query = query.where(RunRow.run_id == run_id)
        async with self.sessions() as session:
            return [RunSummary(*row) for row in (await session.execute(query)).all()]


    async def list_plan_summaries(
        self, workspace_id: str, *, run_id: str | None = None, closure_version: str | None = None,
    ) -> list[PlanSummary]:
        """Project frozen plan identities without loading request or Run bodies."""
        if self.sessions is None:
            plans = {
                (item.run_id, item.closure_version): PlanSummary(item.run_id, item.closure_version, item.snapshot_digest)
                for item in self.execution_requests.values()
                if item.workspace_id == workspace_id
                and item.snapshot_digest
                and (run_id is None or item.run_id == run_id)
                and (closure_version is None or item.closure_version == closure_version)
            }
            return [plans[key] for key in sorted(plans)]
        query = select(
            RunExecutionRequestRow.run_id, RunExecutionRequestRow.closure_version, RunExecutionRequestRow.snapshot_digest,
        ).where(RunExecutionRequestRow.workspace_id == workspace_id,
                RunExecutionRequestRow.snapshot_digest != "").distinct().order_by(
            RunExecutionRequestRow.run_id, RunExecutionRequestRow.closure_version,
        )
        if run_id is not None:
            query = query.where(RunExecutionRequestRow.run_id == run_id)
        if closure_version is not None:
            query = query.where(RunExecutionRequestRow.closure_version == closure_version)
        async with self.sessions() as session:
            return [PlanSummary(*row) for row in (await session.execute(query)).all()]
