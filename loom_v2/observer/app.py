from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from loom_v2.db.session import make_engine
from loom_v2.settings import Settings
from loom_v2.contracts.types import CapabilityHealthReport
from loom_v2.contracts.api import (
    AgentLeaseRequest,
    CapabilityPublishRequest,
    CapabilityHealthRequest,
    CapabilityPackageUploadRequest,
    ContentPutRequest,
    IdempotencyRequest,
    ResolveRequest,
    RunCancelRequest,
    RunResubmitRequest,
    RunSubmissionRequest,
)
from loom_v2.content_store import ContentStore
from loom_v2.contracts.agents import AgentRegistration, DriverCommand
from loom_v2.contracts.types import ResourceRef
from loom_v2.contracts.errors import DomainError
from loom_v2.observer.gateway import ObserverDriverGateway
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
        try:
            yield
        finally:
            await app.state.service_access.http.aclose()
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
        timeout=settings.worker_action_timeout_seconds,
    )
    app.state.slave_gateway = ObserverSlaveGateway(
        app.state.repo,
        workspace_id=settings.workspace_id,
        token=settings.internal_api_secret,
        timeout=settings.worker_action_timeout_seconds,
    )
    # Observer production composition only owns the repository and gateways.
    # Embedded Driver/Slave wiring lives in
    # ``loom_v2.testing.observer`` and is never imported by this module.
    app.state.embedded = False
    app.state.browser_auth = BrowserAuth(app, settings)
    app.state.repo.orchestrator_runtime_available = False
    app.state.slaves = {}
    app.state.workers = {}
    static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    from .grip import GripService
    app.state.grip = GripService(app, settings)
    from .service_access import ObserverServiceAccess
    app.state.service_access = ObserverServiceAccess(app, settings)
    app.state.service_access.install()

    @app.post("/.well-known/grip")
    async def grip(request: Request):
        return await app.state.grip.handle(request)

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
            capabilities["actions"] = sorted(projected.get("actions", set()))
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
        """Relay a fixed Worker action through Observer's active lease."""
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

    @app.get("/api/v1/runtime", response_model=dict[str, Any])
    async def runtime() -> dict[str, Any] | JSONResponse:
        active_driver = await app.state.gateway.active_driver()
        if active_driver is not None:
            return {
                "execution": True,
                "orchestration": True,
                "driver_id": active_driver.get("agent_id"),
                "driver_epoch": str(active_driver.get("epoch")),
            }
        if not app.state.embedded:
            return JSONResponse(status_code=503, content={"code": "driver_unavailable", "retryable": True})
        return {
            "execution": True,
            "orchestration": True,
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
            ref = await app.state.repo.put_content(payload.content, media_type=str(payload.media_type or ""), name=payload.name)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ref.model_dump(mode="json")

    def _execution_submission_view(item: Any, run: Any) -> dict[str, Any]:
        payload = {
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
        if run.state == "awaiting_auth":
            waiting = next((event for event in reversed(run.events) if event.get("phase") == "activation_wait"), None)
            payload["waiting"] = dict((waiting or {}).get("waiting") or {})
        return payload

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

    @app.post("/api/v1/capability-packages")
    async def upload_capability_package(payload: CapabilityPackageUploadRequest) -> dict[str, Any]:
        try:
            package = await app.state.repo.register_workspace_package(
                settings.workspace_id,
                payload.model_dump(mode="json"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409 if "conflict" in str(exc) or "digest" in str(exc) else 422, detail=str(exc)) from exc
        return {**package.model_dump(mode="json"), "resource": package.version_ref}

    @app.get("/api/v1/capability-packages")
    async def capability_packages(run_id: str | None = None, include_abandoned: bool = False) -> list[dict[str, Any]]:
        packages = await app.state.repo.list_capability_packages(run_id=run_id, include_abandoned=include_abandoned, workspace_id=settings.workspace_id)
        result = []
        for package in packages:
            item = await app.state.repo.public_package_payload(package)
            item["activations"] = [activation.model_dump(mode="json") for activation in await app.state.repo.list_package_activations(package)]
            result.append(item)
        return result

    @app.get("/api/v1/action-descriptors")
    async def action_descriptor(resource_id: str, version_or_digest: str) -> dict[str, Any]:
        """Resolve an action descriptor through the public data plane."""
        try:
            ref = ResourceRef(
                resource_id=resource_id,
                version_or_digest=version_or_digest,
                identity_criterion="descriptor_digest",
            )
            descriptor = await app.state.repo.read_action_descriptor(ref)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "action_descriptor_not_found") from exc
        return {"resource_ref": ref.model_dump(mode="json"), "descriptor": descriptor}

    @app.get("/api/v1/runs/{run_id}/capability-packages")
    async def run_capability_packages(run_id: str) -> list[dict[str, Any]]:
        try:
            packages = await app.state.repo.list_capability_packages(run_id=run_id, include_abandoned=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        record = await app.state.repo.get_run(run_id)
        return [{**(await app.state.repo.public_package_payload(package)), "activations": [activation.model_dump(mode="json") for activation in await app.state.repo.list_package_activations(package)]} for package in packages]

    @app.get("/api/v1/capability-packages/{package_ref:path}")
    async def capability_package(package_ref: str) -> dict[str, Any]:
        try:
            package = await app.state.repo.get_capability_package(package_ref, run_id=None)
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        item = await app.state.repo.public_package_payload(package)
        item["activations"] = [
            activation.model_dump(mode="json")
            for activation in await app.state.repo.list_package_activations(package)
        ]
        return item

    @app.post("/api/v1/capability-packages/{package_ref:path}/promote")
    async def promote_capability_package(package_ref: str, payload: CapabilityPublishRequest) -> dict[str, Any]:
        try:
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
            package = await app.state.repo.promote_capability_package(
                package_ref, idempotency_key=payload.idempotency_key, approved_digest=payload.approved_digest,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {**package.model_dump(mode="json"), "resource": package.version_ref}

    @app.post("/api/v1/capability-packages/{package_ref:path}/abandon")
    async def abandon_capability_package(package_ref: str, payload: IdempotencyRequest | None = None) -> dict[str, Any]:
        try:
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id)
            package = await app.state.repo.abandon_capability_package(package_ref, idempotency_key=payload.idempotency_key if payload else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        return {**package.model_dump(mode="json"), "resource": package.version_ref}

    @app.get("/")
    async def home() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
