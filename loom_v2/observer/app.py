from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import json
import httpx
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from loom_v2.db.session import make_engine
from loom_v2.settings import Settings
from loom_v2.contracts.types import CapabilityHealthReport
from loom_v2.contracts.api import (
    AgentLeaseRequest,
    CapabilityActivationRequest,
    CapabilityHealthRequest,
    CapabilityPackageUploadRequest,
    ResourceCreateRequest,
    ContentPutRequest,
    IdempotencyRequest,
    PublicMessageRequest,
    PublicSlave,
    PublicSlaveList,
    ResolveRequest,
    RunCancelRequest,
    RunResubmitRequest,
    RunSubmissionRequest,
)
from loom_v2.content_store import ContentStore
from loom_v2.contracts.agents import AgentRegistration, DriverCommand
from loom_v2.contracts.refs import select_slave_agents
from loom_v2.contracts.types import ResourceRef
from loom_v2.contracts.errors import DomainError
from loom_v2.observer.gateway import ObserverDriverGateway
from loom_v2.observer.dispatcher import ObserverMessageDispatcher
from loom_v2.observer.activation import execute_activation_targets, execute_auth_targets
from loom_v2.observer.browser_auth import BrowserAuth
from loom_v2.observer.slave_gateway import ObserverSlaveGateway, SlaveForwardError
from loom_v2.auth import InternalAuth
from loom_v2.contracts.package_contracts import load_operator_package_contracts

from .repository import ObserverRepository, run_record_payload


def _run_view(record: Any) -> dict[str, Any]:
    return run_record_payload(record, include_snapshots=False)


def create_app(
    repository: ObserverRepository | None = None,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await app.state.repo.init_db()
        await app.state.dispatcher.start()
        try:
            yield
        finally:
            await app.state.dispatcher.stop()
            await app.state.gateway.close()
            for worker in app.state.workers.values():
                with suppress(Exception):
                    await worker.close()
            for slave in app.state.slaves.values():
                with suppress(Exception):
                    await slave.close()
            if app.state.engine is not None:
                await app.state.engine.dispose()

    app = FastAPI(title="Loom v2 Observer", lifespan=lifespan)
    load_operator_package_contracts(settings.package_contract_dir)
    app.state.engine = None
    if repository is None and not settings.database_url.startswith("sqlite+aiosqlite:///:memory:"):
        app.state.engine = make_engine(settings.database_url)
    content_store = repository.content_store if repository is not None else ContentStore.from_settings(settings)
    app.state.repo = repository or ObserverRepository(app.state.engine, content_store=content_store, settings=settings)
    app.state.gateway = ObserverDriverGateway(
        app.state.repo,
        workspace_id=settings.workspace_id,
        internal_api_secret=settings.internal_api_secret,
        timeout=settings.worker_operation_timeout_seconds,
    )
    app.state.slave_gateway = ObserverSlaveGateway(
        app.state.repo,
        workspace_id=settings.workspace_id,
        token=settings.internal_api_secret,
        timeout=settings.worker_operation_timeout_seconds,
    )
    app.state.dispatcher = ObserverMessageDispatcher(
        app.state.repo,
        app.state.gateway,
        workspace_id=settings.workspace_id,
        forward_timeout=settings.observer_forward_timeout_seconds,
    )
    # Observer production composition only owns the repository, gateway and
    # dispatcher.  Embedded Driver/Slave/MCP wiring lives in
    # ``loom_v2.testing.observer`` and is never imported by this module.
    app.state.embedded = False
    app.state.browser_auth = BrowserAuth(app, settings)
    app.state.repo.orchestrator_runtime_available = False
    app.state.provider = None
    app.state.slaves = {}
    app.state.workers = {}
    app.state.driver = None
    app.state.forward_tasks = {}
    static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        return {"ok": True, "service": "observer"}

    require_internal = InternalAuth(settings.internal_api_secret)
    internal_router = APIRouter(dependencies=[Depends(require_internal)])

    @internal_router.post("/internal/v1/agents/register")
    async def register_agent(payload: AgentRegistration) -> dict[str, Any]:
        try:
            lease = await app.state.repo.register_agent(payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return lease.model_dump(mode="json")

    @internal_router.post("/internal/v1/agents/{agent_id}/heartbeat")
    async def heartbeat_agent(agent_id: str, payload: AgentLeaseRequest) -> dict[str, Any]:
        try:
            epoch_value = payload.driver_epoch if payload.driver_epoch is not None else payload.epoch
            if epoch_value is None:
                raise KeyError("epoch")
            if payload.driver_epoch is not None and payload.epoch is not None and payload.driver_epoch != payload.epoch:
                raise ValueError("driver_epoch_mismatch")
            lease = await app.state.repo.heartbeat_agent(
                agent_id,
                payload.instance_id,
                payload.lease_id,
                int(epoch_value),
                workspace_id=payload.workspace_id or settings.workspace_id,
                role=payload.role,
            )
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"missing_field:{exc.args[0]}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return lease.model_dump(mode="json")

    @internal_router.post("/internal/v1/agents/{agent_id}/release")
    async def release_agent(agent_id: str, payload: AgentLeaseRequest) -> dict[str, Any]:
        try:
            epoch_value = payload.driver_epoch if payload.driver_epoch is not None else payload.epoch
            if epoch_value is None:
                raise KeyError("epoch")
            if payload.driver_epoch is not None and payload.epoch is not None and payload.driver_epoch != payload.epoch:
                raise ValueError("driver_epoch_mismatch")
            await app.state.repo.release_agent(
                agent_id,
                payload.instance_id,
                payload.lease_id,
                int(epoch_value),
                workspace_id=payload.workspace_id or settings.workspace_id,
                role=payload.role,
            )
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"missing_field:{exc.args[0]}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"released": True}

    @internal_router.get("/internal/v1/agents/slaves")
    async def list_slaves(workspace_id: str | None = None) -> list[dict[str, Any]]:
        agents = await app.state.repo.refresh_slaves(workspace_id or settings.workspace_id)
        for agent in agents:
            projected = app.state.repo.slave_capabilities.get(str(agent.get("agent_id") or ""))
            if projected is None:
                continue
            capabilities = dict(agent.get("capabilities") or {})
            capabilities["operations"] = sorted(projected.get("operations", set()))
            agent["capabilities"] = capabilities
        # Driver only needs logical identities and capability snapshots.  The
        # Observer-owned relay keeps endpoint URLs out of the Driver plane.
        return [
            {key: value for key, value in agent.items() if key != "endpoint_url"}
            for agent in agents
        ]

    @internal_router.post("/internal/v1/capability-health")
    async def capability_health(payload: CapabilityHealthRequest) -> dict[str, Any]:
        report: CapabilityHealthReport | None = None
        try:
            report = payload.report
            agent_id = payload.agent_id
            workspace_id = payload.workspace_id
            if report.target_slave != agent_id:
                raise ValueError("target_slave_mismatch")
            await app.state.repo.heartbeat_agent(
                agent_id,
                payload.instance_id,
                payload.lease_id,
                payload.epoch,
                workspace_id=workspace_id,
                role="slave",
            )
            # Health reports are scoped by the lease workspace as well as by
            # package identity.  Do this check before mutating the activation
            # projection so a valid Slave lease cannot report on another
            # workspace's package.
            try:
                await app.state.repo._assert_package_workspace(
                    report.package_version_ref,
                    workspace_id,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="capability_package_not_found",
                ) from exc
            activation = await app.state.repo.record_capability_health(report)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"missing_field:{exc.args[0]}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"accepted": True, "activation": activation.model_dump(mode="json")}

    @internal_router.post("/internal/v1/driver/commands")
    async def driver_command(payload: DriverCommand) -> dict[str, Any]:
        async def execute_node_via_observer(
            *,
            workspace_id: str,
            run_id: str,
            node_id: str,
            target: str,
            provision_payload: dict[str, Any],
            dispatch_payload: dict[str, Any],
        ) -> dict[str, Any]:
            """Fence one Attempt and keep the Worker data path Observer-owned."""
            existing_run = await app.state.repo.get_run(run_id)
            existing_node = next(
                (item for item in existing_run.dynamic_nodes if item.node_id == node_id),
                None,
            )
            if existing_node is not None and existing_node.state == "completed":
                existing_attempt = next(
                    (
                        item for item in reversed(existing_run.attempts)
                        if item.get("node_id") == node_id and item.get("state") == "completed"
                    ),
                    None,
                )
                if existing_attempt is None:
                    raise RuntimeError("dynamic_node_result_missing")
                if str(existing_attempt.get("target") or "") != target:
                    raise RuntimeError("node_target_mismatch")
                return {
                    "node": existing_node.model_dump(mode="json"),
                    "attempt": existing_attempt,
                    "terminal_report": {
                        "state": "completed",
                        "attempt_id": existing_attempt.get("attempt_id"),
                        "execution_id": existing_run.execution_id,
                        "execution_epoch": existing_run.execution_epoch,
                        "result": {
                            "resource_ref": existing_attempt.get("result_ref"),
                            "value": existing_attempt.get("value"),
                        },
                    },
                }
            # The Driver's orchestration runtime records the Attempt before
            # asking Observer to execute the immutable Worker envelope.  Do
            # not dispatch that same node a second time: a retry/re-entry may
            # already have moved it to ``dispatched``.  Reuse the active
            # fenced Attempt and keep the relay idempotent.
            current = await app.state.repo.get_run(run_id)
            current_node = next(
                (item for item in current.dynamic_nodes if item.node_id == node_id),
                None,
            )
            if current_node is None:
                raise RuntimeError("dynamic_node_not_found")
            if current_node.state == "accepted":
                attempt = await app.state.repo.dispatch_dynamic_node(
                    run_id, node_id, target=target
                )
            elif current_node.state == "dispatched":
                active = next(
                    (
                        item for item in reversed(current.attempts)
                        if item.get("node_id") == node_id
                        and item.get("state") in {"created", "running"}
                    ),
                    None,
                )
                if active is None or str(active.get("target") or "") != target:
                    raise RuntimeError("dynamic_node_attempt_missing")
                attempt = active
            else:
                raise RuntimeError("dynamic_node_not_dispatchable")
            try:
                provision = await app.state.slave_gateway.forward(
                    target, "/worker/v1/provision", provision_payload
                )
                raw_report = provision.get("health_report") or provision
                report = CapabilityHealthReport.model_validate(raw_report)
                await app.state.repo.record_capability_health(report, run_id=run_id)

                run = await app.state.repo.get_run(run_id)
                request = dict(dispatch_payload)
                request.update(
                    {
                        "attempt_id": attempt["attempt_id"],
                        "execution_id": run.execution_id,
                        "execution_epoch": run.execution_epoch,
                        "workspace_id": workspace_id,
                    }
                )
                envelope = await app.state.slave_gateway.forward(
                    target, "/worker/v1/dispatch", request
                )
                terminal = envelope.get("terminal_report")
                if not isinstance(terminal, dict):
                    raise RuntimeError("worker_dispatch_invalid_response")
                result = terminal.get("result")
                if not isinstance(result, dict) or "value" not in result:
                    raise RuntimeError("worker_dispatch_result_missing")
                completed = await app.state.repo.record_dynamic_node_result(
                    run_id,
                    node_id,
                    {
                        "attempt_id": attempt["attempt_id"],
                        "execution_id": run.execution_id,
                        "execution_epoch": run.execution_epoch,
                        "value": result["value"],
                        "terminal_state": terminal.get("state", "completed"),
                        "terminal_error": terminal.get("error"),
                        "validation_evidence": terminal.get("validation_evidence") or [],
                    },
                )
                updated = await app.state.repo.get_run(run_id)
                stored_attempt = next(
                    item for item in updated.attempts
                    if item.get("attempt_id") == attempt["attempt_id"]
                )
                return {
                    "node": completed.model_dump(mode="json"),
                    "attempt": stored_attempt,
                    "terminal_report": terminal,
                }
            except Exception as exc:
                with suppress(Exception):
                    await app.state.repo.fail_dynamic_node(
                        run_id, node_id,
                        attempt_id=attempt.get("attempt_id"),
                        error={"code": str(exc) or "node_execution_failed"},
                    )
                raise

        try:
            result = await app.state.repo.execute_driver_command(
                payload,
                node_executor=execute_node_via_observer,
            )
        except DomainError as exc:
            envelope = exc.envelope.model_dump(mode="json")
            raise HTTPException(status_code=409, detail=envelope) from exc
        except ValueError as exc:
            code = str(exc)
            status = 400 if code == "driver_command_not_allowed" else 409
            raise HTTPException(status_code=status, detail=code) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"missing_field:{exc.args[0]}") from exc
        return result

    @internal_router.post("/internal/v1/slaves/{slave_id}/forward")
    async def forward_slave(slave_id: str, payload: dict[str, Any]) -> Response:
        """Relay a fixed Worker operation through Observer's active lease."""
        path = payload.get("path")
        request_payload = payload.get("payload")
        allowed = {
            "/worker/v1/provision", "/worker/v1/dispatch",
            "/worker/v1/deprovision", "/worker/v1/capability/auth/start",
            "/worker/v1/capability/auth/browser", "/worker/v1/inspect",
            "/worker/v1/reconcile",
        }
        if path not in allowed or not isinstance(request_payload, dict):
            raise HTTPException(status_code=400, detail="slave_forward_request_invalid")
        try:
            body = await app.state.slave_gateway.forward(slave_id, path, request_payload)
        except SlaveForwardError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.body)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(content=body)

    @internal_router.post("/driver-gateway/v1/registration-check")
    async def registration_check() -> dict[str, Any]:
        driver = await app.state.gateway.active_driver()
        return {"registered": driver is not None, "driver": driver}

    app.include_router(internal_router)

    @app.get("/api/v1/runtime", response_model=dict[str, str | None])
    async def runtime() -> dict[str, str | None] | JSONResponse:
        active_driver = await app.state.gateway.active_driver()
        if active_driver is not None:
            capabilities = active_driver.get("capabilities") or {}
            return {
                "coding_agent_backend": str(capabilities.get("backend") or "codex"),
                "coding_agent_label": str(capabilities.get("label") or "Codex app-server"),
                "model": capabilities.get("model") or settings.codex_model,
                "driver_id": active_driver.get("agent_id"),
                "driver_epoch": str(active_driver.get("epoch")),
            }
        if not app.state.embedded:
            return JSONResponse(status_code=503, content={"code": "driver_unavailable", "retryable": True})
        backend = settings.coding_agent_backend.strip().lower()
        if backend == "codex":
            label = "Codex app-server"
            model = settings.codex_model
        elif backend == "fake":
            label = "Fake coding agent"
            model = None
        else:
            label = backend or "Unknown coding agent"
            model = settings.codex_model or None
        return {
            "coding_agent_backend": backend,
            "coding_agent_label": label,
            "model": model,
        }

    @app.get("/api/v1/content/{digest}")
    async def content(digest: str) -> Response:
        try:
            body = await app.state.repo.content_store.get(digest)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="content_not_found") from exc
        return Response(content=body, media_type="application/octet-stream", headers={"X-Content-Digest": digest})

    @app.post("/api/v1/content")
    async def put_content(payload: ContentPutRequest) -> dict[str, Any]:
        if "content" not in payload.model_fields_set:
            raise HTTPException(status_code=400, detail="content_required")
        try:
            ref = await app.state.repo.put_content(payload.content, media_type=str(payload.media_type or ""))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ref.model_dump(mode="json")

    def _execution_submission_view(item: Any, run: Any) -> dict[str, Any]:
        return {
            "request_id": item.request_id,
            "run_id": item.run_id,
            "closure_version": item.closure_version,
            "snapshot_digest": item.snapshot_digest,
            "closure_contract": item.closure_contract,
            "current_run_state": run.state,
            "state": (item.outcome or {}).get("disposition", run.state) if item.state == "settled" else run.state,
            "execution_request_state": item.state,
            "source": item.source,
            "conversation_ref": item.conversation_ref,
            "execution_id": item.execution_id,
            "execution_epoch": item.execution_epoch,
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "deadline_at": item.deadline_at.isoformat() if item.deadline_at else None,
            "outcome": item.outcome,
        }

    @app.post("/api/v1/run-submissions", status_code=202)
    async def submit_run_plan(payload: RunSubmissionRequest) -> JSONResponse:
        try:
            item = await app.state.repo.submit_run_plan(
                workspace_id=settings.workspace_id,
                request_id=payload.request_id,
                plan=payload.plan,
                conversation_ref=payload.conversation_ref,
                source="external",
            )
            run = await app.state.repo.get_run(item.run_id)
            return JSONResponse(status_code=202, content=_execution_submission_view(item, run))
        except DomainError as exc:
            raise HTTPException(status_code=409, detail=exc.envelope.model_dump(mode="json")) from exc
        except ValueError as exc:
            code = str(exc)
            status = 409 if code in {"request_id_reused", "workspace_binding_mismatch", "readiness_blocked"} else 422
            detail = exc.args[0] if code != "readiness_blocked" else code
            raise HTTPException(status_code=status, detail=detail) from exc

    @app.get("/api/v1/run-submissions/{request_id}")
    async def get_run_submission(request_id: str) -> dict[str, Any]:
        try:
            item = await app.state.repo.get_execution_request(settings.workspace_id, request_id)
            run = await app.state.repo.get_run(item.run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_submission_not_found") from exc
        return _execution_submission_view(item, run)

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel_submitted_run(run_id: str, payload: RunCancelRequest) -> dict[str, Any]:
        try:
            run = await app.state.repo._assert_run_workspace(run_id, settings.workspace_id)
            if payload.request_id:
                item = await app.state.repo.get_execution_request(settings.workspace_id, payload.request_id)
                if item.run_id != run_id:
                    raise ValueError("request_run_mismatch")
            run = await app.state.repo.cancel_run(run_id, payload.reason)
            return run_record_payload(run, include_snapshots=False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/runs/{run_id}/resubmit", status_code=202)
    async def resubmit_submitted_run(run_id: str, payload: RunResubmitRequest) -> JSONResponse:
        try:
            item = await app.state.repo.resubmit_run_plan(
                workspace_id=settings.workspace_id,
                run_id=run_id,
                request_id=payload.request_id,
                base_closure_version=payload.base_closure_version,
                plan=payload.plan,
            )
            run = await app.state.repo.get_run(run_id)
            return JSONResponse(status_code=202, content=_execution_submission_view(item, run))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except DomainError as exc:
            raise HTTPException(status_code=409, detail=exc.envelope.model_dump(mode="json")) from exc
        except ValueError as exc:
            code = str(exc)
            status = 409 if code in {
                "request_id_reused", "version_conflict", "resubmit_not_repairable",
                "planning_source_mismatch", "workspace_binding_mismatch", "success_contract_changed",
                "constraints_changed", "execution_policy_changed", "closure_goal_mismatch",
            } else 422
            raise HTTPException(status_code=status, detail=code) from exc

    @app.post("/api/v1/messages", response_model=dict[str, Any])
    async def message(payload: PublicMessageRequest) -> dict[str, Any] | JSONResponse:
        if not isinstance(payload.text, str) or not payload.text.strip():
            raise HTTPException(status_code=422, detail="text_required")
        if not app.state.embedded and not str(payload.request_id or "").strip():
            raise HTTPException(status_code=422, detail="request_id_required")
        if not app.state.embedded and not str(payload.conversation_ref or "").strip():
            raise HTTPException(status_code=422, detail="conversation_ref_required")
        if not app.state.embedded and (payload.workspace_id or settings.workspace_id) != settings.workspace_id:
            raise HTTPException(status_code=403, detail="workspace_binding_mismatch")
        conversation_ref = payload.conversation_ref or "conversation-default"
        supplied_request_id = bool(payload.request_id)
        request_id = payload.request_id or f"request-{uuid4().hex}"
        if not app.state.embedded:
            # A standalone Observer without an internal control-plane secret
            # has no safe way to deliver work when no Driver is registered.
            # Keep the synchronous failure contract for local deployments;
            # split deployments with a secret may durably queue the receipt
            # for a Driver that will register later.
            if not settings.internal_api_secret.strip() and await app.state.gateway.active_driver() is None:
                return JSONResponse(status_code=503, content={"code": "driver_unavailable", "retryable": True})
            try:
                existing = await app.state.repo.get_message_receipt(settings.workspace_id, request_id)
                receipt = await app.state.repo.create_or_get_message_receipt(
                    settings.workspace_id,
                    request_id,
                    conversation_ref,
                    payload.text,
                )
            except ValueError as exc:
                if str(exc) == "request_id_reused":
                    raise HTTPException(status_code=409, detail="request_id_reused") from exc
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if existing is not None and receipt.state == "accepted":
                receipt = await app.state.repo.queue_message_receipt(settings.workspace_id, request_id)
            app.state.dispatcher.wake()
            body = {"accepted": True, "conversation_ref": conversation_ref, "request_id": request_id, "status": receipt.state}
            return JSONResponse(status_code=202, content=body)
        active_driver = await app.state.gateway.active_driver()
        if active_driver is not None:
            pending = app.state.forward_tasks.get(request_id)
            if pending is not None and not pending.done():
                return JSONResponse(status_code=202, content={"accepted": True, "conversation_ref": conversation_ref, "request_id": request_id, "status": "in_flight"})

            async def _forward_legacy() -> dict[str, Any] | None:
                try:
                    return await app.state.gateway.forward(
                        "/driver/v1/messages",
                        {
                            **payload.model_dump(exclude_none=True),
                            "conversation_ref": conversation_ref,
                            "request_id": request_id,
                            "workspace_id": settings.workspace_id,
                        },
                        timeout=settings.observer_forward_timeout_seconds,
                    )
                except Exception:
                    return None

            task = asyncio.create_task(_forward_legacy())
            app.state.forward_tasks[request_id] = task
            task.add_done_callback(lambda done, rid=request_id: app.state.forward_tasks.pop(rid, None) if app.state.forward_tasks.get(rid) is done else None)
            return JSONResponse(status_code=202, content={"accepted": True, "conversation_ref": conversation_ref, "request_id": request_id, "status": "accepted"})
        if supplied_request_id:
            return JSONResponse(status_code=503, content={"code": "driver_unavailable", "retryable": True})
        if app.state.driver is None:
            return JSONResponse(status_code=503, content={"code": "driver_unavailable", "retryable": True})
        try:
            return await app.state.driver.run_prompt(conversation_ref, payload.text)
        except (RuntimeError, FileNotFoundError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, dict):
                reason = dict(reason)
                code = str(reason.get("code") or "coding_agent_error")
                retryable = code not in {"coding_agent_blocked", "coding_agent_usage_limited", "coding_agent_budget_limited"}
                return JSONResponse(status_code=503, content={"code": code, "retryable": retryable, "details": reason})
            return JSONResponse(status_code=503, content={"code": str(exc), "retryable": True})

    @app.get("/api/v1/conversations")
    async def conversations() -> list[dict[str, Any]]:
        return await app.state.repo.list_conversations(settings.workspace_id)

    @app.get("/api/v1/conversations/{conversation_ref}")
    async def conversation(conversation_ref: str) -> dict[str, Any]:
        try:
            return await app.state.repo.get_conversation(conversation_ref, settings.workspace_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="conversation_not_found") from exc

    @app.post("/api/v1/conversations/{conversation_ref}/interrupt")
    async def interrupt_conversation(conversation_ref: str) -> dict[str, Any]:
        if not app.state.embedded:
            receipts = await app.state.repo.list_message_receipts(settings.workspace_id, conversation_ref)
            pending = next((item for item in reversed(receipts) if item.state in {"accepted", "queued", "retryable", "in_flight"}), None)
            if pending is None:
                raise HTTPException(status_code=409, detail="conversation_not_active")
            if pending.state != "in_flight":
                interrupted = await app.state.repo.interrupt_message_receipt(settings.workspace_id, pending.request_id)
                return {"conversation_ref": conversation_ref, "request_id": interrupted.request_id, "status": "interrupted"}
            try:
                return await app.state.gateway.forward(
                    f"/driver/v1/conversations/{conversation_ref}/interrupt",
                    {"conversation_ref": conversation_ref, "workspace_id": settings.workspace_id, "request_id": pending.request_id},
                )
            except RuntimeError as exc:
                code = str(exc)
                raise HTTPException(status_code=409 if code == "stale_driver_epoch" else 503, detail=code) from exc
        active_driver = await app.state.gateway.active_driver()
        if active_driver is not None:
            try:
                return await app.state.gateway.forward(f"/driver/v1/conversations/{conversation_ref}/interrupt", {"conversation_ref": conversation_ref, "workspace_id": settings.workspace_id})
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        if app.state.driver is None:
            raise HTTPException(status_code=503, detail="driver_unavailable")
        try:
            return await app.state.driver.interrupt(conversation_ref)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/conversations/{conversation_ref}/stream")
    async def stream(conversation_ref: str) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                conversation_view = await app.state.repo.get_conversation(conversation_ref, settings.workspace_id)
            except KeyError:
                return
            for event in conversation_view["events"]:
                yield f"data: {json.dumps(event, sort_keys=True, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/v1/runs/{run_id}/readiness")
    async def readiness(run_id: str) -> dict[str, Any]:
        try:
            await app.state.repo._assert_run_workspace(run_id, settings.workspace_id)
            return await app.state.repo.inspect_readiness(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/runs/{run_id}/close")
    async def close_run(run_id: str) -> dict[str, Any]:
        try:
            await app.state.repo._assert_run_workspace(run_id, settings.workspace_id)
            return _run_view(await app.state.repo.close_run(run_id))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/runs/{run_id}/resolve")
    async def resolve_run(run_id: str, payload: ResolveRequest) -> dict[str, Any]:
        decision = str(payload.decision or "")
        if decision not in {"accept", "abandon"}:
            raise HTTPException(status_code=422, detail="invalid_decision")
        try:
            await app.state.repo._assert_run_workspace(run_id, settings.workspace_id)
            return _run_view(await app.state.repo.resolve_run(run_id, decision))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            await app.state.repo._assert_run_workspace(run_id, settings.workspace_id)
            return _run_view(await app.state.repo.get_run(run_id))
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc

    @app.get("/api/v1/slaves", response_model=PublicSlaveList)
    async def public_slaves(
        available_only: bool = False, workspace_id: str | None = None,
    ) -> PublicSlaveList:
        # Public access is bound to this Observer's workspace, not an arbitrary
        # caller-supplied registry scope. Do not mutate the dispatcher's cache.
        if workspace_id is not None and workspace_id != settings.workspace_id:
            raise HTTPException(status_code=403, detail="workspace_binding_mismatch")
        agents = await app.state.repo.list_agents(settings.workspace_id, role="slave")
        # Prefer active instances; when all are offline show the latest report.
        agents.sort(key=lambda agent: str(agent.get("last_seen_at") or ""), reverse=True)
        selected = select_slave_agents(agents)
        slaves = []
        for slave_id, agent in sorted(selected.items()):
            available = agent.get("lease_state") == "active"
            if available_only and not available:
                continue
            caps = agent.get("capabilities") or {}
            slaves.append(PublicSlave(
                slave_id=slave_id,
                available=available,
                lease_state=agent["lease_state"],
                last_seen_at=agent.get("last_seen_at"),
                base_operations=sorted(caps.get("base_operations", caps.get("operations", []))),
                executor_descriptors=caps.get("executor_descriptors", []),
                runtime_plugin_descriptors=caps.get("runtime_plugin_descriptors", caps.get("runtime_plugins", [])),
                term_support=caps.get("term_support", []),
            ))
        return PublicSlaveList(workspace_id=settings.workspace_id, slaves=slaves)

    @app.get("/api/v1/capabilities")
    async def capabilities() -> list[dict[str, Any]]:
        await app.state.repo.refresh_slaves(settings.workspace_id)
        registered = list(app.state.repo.slave_agents.values())
        if registered:
            records = await app.state.repo._all_records()
            result: list[dict[str, Any]] = []
            for agent in registered:
                caps = agent.get("capabilities") or {}
                active_activations = [
                    activation.model_dump(mode="json")
                    for record in records
                    for activation in record.capability_activations
                    if activation.target_slave == agent["agent_id"]
                    and activation.activation_state == "ready"
                ]
                for package in await app.state.repo.list_capability_packages(
                    workspace_id=settings.workspace_id,
                    include_abandoned=True,
                ):
                    active_activations.extend(
                        activation.model_dump(mode="json")
                        for activation in await app.state.repo.list_package_activations(package)
                        if activation.target_slave == agent["agent_id"]
                        and activation.activation_state == "ready"
                    )
                result.append(
                    {
                        "slave_id": agent["agent_id"],
                        "available": agent.get("lease_state") == "active",
                        "replica": "ready" if agent.get("lease_state") == "active" else "unavailable",
                        "operations": sorted(app.state.repo.slave_capabilities.get(str(agent["agent_id"]), {}).get("operations", set())),
                        "term_support": caps.get("term_support", []),
                        "activations": active_activations,
                        "executor_descriptors": caps.get("executor_descriptors", []),
                        "runtime_plugin_descriptors": caps.get("runtime_plugin_descriptors", caps.get("runtime_plugins", [])),
                    }
                )
            return result
        if not app.state.embedded:
            return []
        records = await app.state.repo._all_records()
        result = []
        for slave_id, slave in app.state.slaves.items():
            activation_map = {
                (activation.package_version_ref, activation.target_slave): activation
                for activation in slave.activations.values()
            }
            for record in records:
                for activation in record.capability_activations:
                    if activation.target_slave == slave_id and activation.activation_state == "ready":
                        activation_map.setdefault(
                            (activation.package_version_ref, activation.target_slave),
                            activation,
                        )
            for package in await app.state.repo.list_capability_packages(
                workspace_id=settings.workspace_id,
                include_abandoned=True,
            ):
                for activation in await app.state.repo.list_package_activations(package):
                    if activation.target_slave == slave_id and activation.activation_state == "ready":
                        activation_map.setdefault(
                            (activation.package_version_ref, activation.target_slave),
                            activation,
                        )
            result.append(
                {
                    "slave_id": slave_id,
                    "available": slave.available,
                    "replica": slave.replica.state,
                    "operations": sorted(slave.capability_snapshot()),
                    "term_support": [support.model_dump(mode="json") for support in slave.term_support()],
                    "activations": [activation.model_dump(mode="json") for activation in activation_map.values()],
                    "executor_descriptors": [{"package_type": descriptor.package_type, "kind": descriptor.kind, "version": descriptor.version, "operations": sorted(descriptor.operations), "descriptor_ref": descriptor.descriptor_ref, "digest": descriptor.digest} for descriptor in slave.executor_registry.descriptors()],
                    "runtime_plugin_descriptors": [descriptor.__dict__ | {"supports": [support.to_mapping() for support in descriptor.supports]} for descriptor in slave.runtime_plugin_host.descriptors()] if slave.runtime_plugin_host is not None else [],
                }
            )
        return result

    @app.post("/api/v1/capability-packages")
    async def upload_capability_package(payload: CapabilityPackageUploadRequest) -> dict[str, Any]:
        try:
            package = await app.state.repo.register_workspace_package(
                settings.workspace_id,
                payload.model_dump(mode="json"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409 if "conflict" in str(exc) or "digest" in str(exc) else 422, detail=str(exc)) from exc
        return package.model_dump(mode="json")

    @app.get("/api/v1/capability-packages")
    async def capability_packages(run_id: str | None = None, include_abandoned: bool = False) -> list[dict[str, Any]]:
        packages = await app.state.repo.list_capability_packages(run_id=run_id, include_abandoned=include_abandoned, workspace_id=settings.workspace_id)
        result = []
        for package in packages:
            item = package.model_dump(mode="json")
            item["activations"] = [activation.model_dump(mode="json") for activation in await app.state.repo.list_package_activations(package)]
            result.append(item)
        return result

    @app.post("/api/v1/resources")
    async def register_resource(payload: ResourceCreateRequest) -> dict[str, Any]:
        try:
            resource = await app.state.repo.register_resource(
                settings.workspace_id,
                payload.model_dump(mode="json"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="resource_reference_not_found") from exc
        except ValueError as exc:
            status = 409 if "conflict" in str(exc) or "digest" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return resource.model_dump(mode="json")

    @app.get("/api/v1/resources")
    async def list_resources() -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in await app.state.repo.list_resources(settings.workspace_id)
        ]

    @app.get("/api/v1/resources/{resource_id:path}")
    async def get_resource(resource_id: str) -> dict[str, Any]:
        try:
            resource = await app.state.repo.get_resource(settings.workspace_id, resource_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="resource_not_found") from exc
        return resource.model_dump(mode="json")

    @app.get("/api/v1/runs/{run_id}/capability-packages")
    async def run_capability_packages(run_id: str) -> list[dict[str, Any]]:
        try:
            packages = await app.state.repo.list_capability_packages(run_id=run_id, include_abandoned=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        record = await app.state.repo.get_run(run_id)
        return [{**package.model_dump(mode="json"), "activations": [activation.model_dump(mode="json") for activation in await app.state.repo.list_package_activations(package)]} for package in packages]

    @app.get("/api/v1/capability-packages/{package_ref:path}")
    async def capability_package(package_ref: str) -> dict[str, Any]:
        try:
            package = await app.state.repo.get_capability_package(package_ref, run_id=None)
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        item = package.model_dump(mode="json")
        item["activations"] = [
            activation.model_dump(mode="json")
            for activation in await app.state.repo.list_package_activations(package)
        ]
        return item

    @app.post("/api/v1/capability-packages/{package_ref:path}/promote")
    async def promote_capability_package(package_ref: str, payload: CapabilityActivationRequest | None = None) -> dict[str, Any]:
        payload = payload or CapabilityActivationRequest()
        try:
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
            package = await app.state.repo.promote_capability_package(package_ref, idempotency_key=payload.idempotency_key, approved_digest=payload.approved_digest)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        targets = payload.target_slaves
        # Publishing an interface package is supported, but it has no runtime
        # implementation and therefore can never be activated on a target.
        if targets and package.is_interface:
            raise HTTPException(status_code=409, detail="capability_package_not_executable")
        reports = await execute_activation_targets(
            app.state.repo,
            package,
            targets,
            desired_state="running",
            workspace_id=settings.workspace_id,
            workers=app.state.workers,
            slaves=app.state.slaves,
            compute_binding=payload.compute_binding.model_dump(mode="json") if payload.compute_binding else None,
            idempotency_key=payload.idempotency_key,
            instance_name=payload.instance_name,
            target_resource_ref=payload.target_resource_ref.model_dump(mode="json") if payload.target_resource_ref else None,
            instance_config=payload.instance_config,
            slave_gateway=None if app.state.embedded else app.state.slave_gateway,
        )
        return {"package": package.model_dump(mode="json"), "health_reports": reports}

    @app.post("/api/v1/capability-packages/{package_ref:path}/deactivate")
    async def deactivate_capability_package(package_ref: str, payload: CapabilityActivationRequest | None = None) -> dict[str, Any]:
        payload = payload or CapabilityActivationRequest()
        try:
            package = await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
            approved_digest = payload.approved_digest
            if not approved_digest:
                raise ValueError("approved_digest_required")
            if str(approved_digest).lower() != package.package_digest.lower():
                raise ValueError("capability_package_digest_mismatch")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        targets = payload.target_slaves
        if not targets:
            raise HTTPException(status_code=400, detail="target_slave_required")
        reports = await execute_activation_targets(
            app.state.repo,
            package,
            targets,
            desired_state="stopped",
            workspace_id=settings.workspace_id,
            workers=app.state.workers,
            slaves=app.state.slaves,
            idempotency_key=payload.idempotency_key,
            instance_name=payload.instance_name,
            target_resource_ref=payload.target_resource_ref.model_dump(mode="json") if payload.target_resource_ref else None,
            instance_config=payload.instance_config,
            slave_gateway=None if app.state.embedded else app.state.slave_gateway,
        )
        return {"package": package.model_dump(mode="json"), "health_reports": reports}

    @app.post("/api/v1/capability-packages/{package_ref:path}/auth/start")
    async def start_capability_auth(package_ref: str, request: Request, payload: CapabilityActivationRequest | None = None) -> dict[str, Any]:
        payload = payload or CapabilityActivationRequest()
        try:
            package = await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
            if not payload.approved_digest:
                raise ValueError("approved_digest_required")
            if payload.approved_digest.lower() != package.package_digest.lower():
                raise ValueError("capability_package_digest_mismatch")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        targets = payload.target_slaves
        if not targets:
            raise HTTPException(status_code=400, detail="target_slave_required")
        authentication = (package.body or {}).get("authentication") if isinstance(package.body, dict) else None
        browser_path = authentication.get("browser_path") if isinstance(authentication, dict) else None
        if browser_path:
            return await app.state.browser_auth.start(request, package, targets, payload.instance_name)
        reports = await execute_auth_targets(
            app.state.repo,
            package,
            targets,
            workspace_id=settings.workspace_id,
            workers=app.state.workers,
            slaves=app.state.slaves,
            approved_digest=package.package_digest,
            instance_name=payload.instance_name,
            slave_gateway=None if app.state.embedded else app.state.slave_gateway,
        )
        return {"package": package.model_dump(mode="json"), "health_reports": reports}


    @app.post("/api/v1/capability-packages/{package_ref:path}/abandon")
    async def abandon_capability_package(package_ref: str, payload: IdempotencyRequest | None = None) -> dict[str, Any]:
        try:
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
            package = await app.state.repo.abandon_capability_package(package_ref, idempotency_key=payload.idempotency_key if payload else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        return package.model_dump(mode="json")

    @app.get("/")
    async def home() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
