from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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
    RunActivationStopRequest,
)
from loom_v2.content_store import ContentStore
from loom_v2.contracts.agents import AgentRegistration, DriverCommand
from loom_v2.contracts.types import ResourceRef
from loom_v2.contracts.errors import DomainError
from loom_v2.observer.gateway import ObserverDriverGateway
from loom_v2.observer.browser_auth import BrowserAuth
from loom_v2.observer.slave_gateway import ObserverSlaveGateway
from loom_v2.auth import InternalAuth
from loom_v2.contracts.package_contracts import load_operator_package_contracts

from .repository import ObserverRepository
from loom_v2.pool.repository import PoolRepository
from loom_v2.pool.client import invoke, TASK_METHODS, RESOURCE_METHODS
from .downloads import client_downloads


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
            if not isinstance(app.state.repo, PoolRepository):
                await app.state.repo.tasks.close()
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
    download_dir = Path(settings.client_download_dir) if settings.client_download_dir else static_dir.parent / "downloads"
    app.include_router(client_downloads(download_dir))

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
        return {"ok": True, "service": "pool" if isinstance(app.state.repo, PoolRepository) else "observer"}

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
        # Endpoints are supplied by authenticated Slave registration.
        from loom_v2.execution_binding import execution_offer
        for agent in agents:
            agent["execution_offer"] = execution_offer(app.state.repo.names, agent["endpoint_url"])
        return agents

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
                    run_id=payload.run_id,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="capability_package_not_found",
                ) from exc
            activation = await app.state.repo.record_capability_health(report, run_id=payload.run_id)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"missing_field:{exc.args[0]}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"accepted": True, "activation": activation.model_dump(mode="json")}

    @internal_router.post("/internal/v1/driver/commands")
    async def driver_command(payload: DriverCommand) -> dict[str, Any]:
        try:
            result = await app.state.repo.execute_driver_command(payload)
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


    @internal_router.post("/driver-gateway/v1/registration-check")
    async def registration_check() -> dict[str, Any]:
        driver = await app.state.gateway.active_driver()
        return {"registered": driver is not None, "driver": driver}

    is_pool = isinstance(app.state.repo, PoolRepository)

    @internal_router.post("/internal/v1/tasks" if is_pool else "/internal/v1/resources/commands")
    async def repository_command(body: dict):
        return await invoke(app.state.repo, body, TASK_METHODS if is_pool else RESOURCE_METHODS, native=app.state.grip.native)

    if is_pool:
        @internal_router.post("/internal/v1/attempts/authorize")
        async def authorize_attempt(body: dict):
            try:
                return await app.state.repo.authorize_attempt(body)
            except (KeyError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc

    if not is_pool:
        internal_router.routes = [route for route in internal_router.routes
            if route.path != "/internal/v1/driver/commands"]
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
        try:
            packages = await app.state.repo.list_capability_packages(run_id=run_id, include_abandoned=include_abandoned, workspace_id=settings.workspace_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        result = []
        for package in packages:
            item = await app.state.repo.public_package_payload(package)
            item["activations"] = [activation.model_dump(mode="json") for activation in await app.state.repo.list_package_activations(package, refresh=False)]
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
            packages = await app.state.repo.list_capability_packages(run_id=run_id, include_abandoned=True, workspace_id=settings.workspace_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        return [{**(await app.state.repo.public_package_payload(package)), "activations": [activation.model_dump(mode="json") for activation in await app.state.repo.list_package_activations(package, refresh=False)]} for package in packages]

    @app.get("/api/v1/capability-packages/{package_ref:path}")
    async def capability_package(package_ref: str, run_id: str | None = None) -> dict[str, Any]:
        try:
            package = await app.state.repo.get_capability_package(package_ref, run_id=run_id)
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id, run_id=run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        item = await app.state.repo.public_package_payload(package)
        item["activations"] = [
            activation.model_dump(mode="json")
            for activation in await app.state.repo.list_package_activations(package)
        ]
        return item

    @app.post("/api/v1/runs/{run_id}/capability-packages/{package_ref:path}/stop")
    async def stop_run_activation(run_id: str, package_ref: str, payload: RunActivationStopRequest) -> dict[str, Any]:
        from .activation import execute_activation_targets
        try:
            package = await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id, run_id=run_id)
            if package.scope != "run_bound" or package.source_run_ref != run_id:
                raise ValueError("run_package_owner_required")
            reports = await execute_activation_targets(
                app.state.repo, package, [payload.target_slave], desired_state="stopped",
                workspace_id=settings.workspace_id, workers=app.state.workers, slaves=app.state.slaves,
                slave_gateway=app.state.slave_gateway, instance_name=payload.instance_name,
                idempotency_key=payload.idempotency_key, expected_revision=payload.expected_revision,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"health_reports": reports}

    @app.post("/api/v1/capability-packages/{package_ref:path}/promote")
    async def promote_capability_package(package_ref: str, payload: CapabilityPublishRequest, run_id: str | None = None) -> dict[str, Any]:
        try:
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id, run_id=run_id)
            package = await app.state.repo.promote_capability_package(
                package_ref, run_id=run_id, idempotency_key=payload.idempotency_key, approved_digest=payload.approved_digest,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {**package.model_dump(mode="json"), "resource": package.version_ref}

    @app.post("/api/v1/capability-packages/{package_ref:path}/abandon")
    async def abandon_capability_package(package_ref: str, payload: IdempotencyRequest | None = None, run_id: str | None = None) -> dict[str, Any]:
        try:
            await app.state.repo._assert_package_workspace(package_ref, settings.workspace_id, run_id=run_id)
            package = await app.state.repo.abandon_capability_package(package_ref, run_id=run_id, idempotency_key=payload.idempotency_key if payload else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="capability_package_not_found") from exc
        return {**package.model_dump(mode="json"), "resource": package.version_ref}

    @app.get("/")
    async def home() -> Response:
        if not is_pool and settings.pool_public_base_url:
            return RedirectResponse(settings.pool_public_base_url.rstrip("/") + "/")
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
