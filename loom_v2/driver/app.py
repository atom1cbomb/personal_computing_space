from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import uuid4
from typing import Any, cast

from fastapi import FastAPI

from loom_v2.contracts.agents import AgentRegistration
from loom_v2.settings import Settings
from loom_v2.content_store import ContentStore

from .control_client import ObserverControlClient
from .service import DriverService
from .worker import WorkerSession
from loom_v2.contracts.package_contracts import load_operator_package_contracts


def create_app(
    *,
    settings: Settings | None = None,
    observer_transport: Any | None = None,
    control_client: ObserverControlClient | None = None,
    provider: Any | None = None,
) -> FastAPI:
    settings = settings or Settings()
    load_operator_package_contracts(settings.package_contract_dir)
    # Driver is execution-only.  The optional argument is retained solely so
    # old embedding code can construct the app; no provider is created or
    # invoked by the production Driver process.
    provider = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        app.state.heartbeat_task = asyncio.create_task(registration_loop())
        app.state.execution_task = app.state.service.start_execution_loop()
        # Give the first registration a scheduling opportunity so health probes
        # observe a lease immediately when Observer is up.
        await asyncio.sleep(0)
        try:
            yield
        finally:
            task = app.state.heartbeat_task
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            execution_task = app.state.execution_task
            if execution_task is not None:
                await app.state.service.stop_execution_loop()
                execution_task.cancel()
                with suppress(asyncio.CancelledError):
                    await execution_task
            if app.state.lease is not None:
                with suppress(Exception):
                    await control.release()
            with suppress(Exception):
                await control.close()
            for worker in workers.values():
                with suppress(Exception):
                    await worker.close()

    app = FastAPI(title="Loom v2 Driver", lifespan=lifespan)
    instance_id = settings.agent_instance_id or f"driver-{uuid4().hex[:12]}"
    control = control_client or ObserverControlClient(
        settings.observer_url,
        driver_id=settings.agent_id,
        instance_id=instance_id,
        workspace_id=settings.workspace_id,
        internal_api_secret=settings.internal_api_secret,
        timeout=settings.worker_action_timeout_seconds,
        transport=observer_transport,
        settings=settings,
    )
    workers: dict[str, WorkerSession] = {}
    app.state.control = control
    app.state.provider = provider
    content_store = ContentStore.from_settings(settings)
    app.state.service = DriverService(cast(Any, control), provider, workers=workers, deadline_seconds=settings.coding_agent_deadline_seconds, content_store=content_store, settings=settings)
    app.state.lease = None
    app.state.heartbeat_task = None
    app.state.execution_task = None

    async def registration_loop() -> None:
        while True:
            try:
                registration = AgentRegistration(
                    role="driver",
                    agent_id=settings.agent_id,
                    instance_id=instance_id,
                    workspace_id=settings.workspace_id,
                    endpoint_url=settings.driver_url or "http://driver:8090",
                    protocol_version=settings.agent_protocol_version,
                    capabilities={"execution": True, "orchestration": True},
                )
                app.state.lease = await control.register(registration)
                if app.state.service.coordinator is not None:
                    app.state.service.coordinator.driver_epoch = app.state.lease.epoch
                with suppress(Exception):
                    await control.command("execution.recover", {"workspace_id": settings.workspace_id})
                with suppress(Exception):
                    await control.command("run.recovery.list", {"workspace_id": settings.workspace_id})
                break
            except Exception:
                await asyncio.sleep(max(0.1, settings.agent_registration_retry_seconds))
        while True:
            await asyncio.sleep(max(0.1, app.state.lease.heartbeat_interval_seconds if app.state.lease else settings.agent_heartbeat_interval_seconds))
            try:
                await control.heartbeat()
            except Exception:
                # A later registration obtains a new epoch and fences this
                # instance if another Driver has taken ownership.
                await app.state.service.stop_execution_loop()
                try:
                    app.state.lease = await control.register()
                    if app.state.service.coordinator is not None:
                        app.state.service.coordinator.driver_epoch = app.state.lease.epoch
                    await control.command("execution.recover", {})
                    app.state.execution_task = app.state.service.start_execution_loop()
                except Exception:
                    continue

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        lease = app.state.lease
        return {"ok": lease is not None, "service": "driver", "agent_id": settings.agent_id, "epoch": lease.epoch if lease else None}

    return app


app = create_app()
