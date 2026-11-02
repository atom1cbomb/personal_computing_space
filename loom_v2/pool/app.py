"""Driver Pool API: durable submissions, Run state and worker coordination."""
from loom_v2.settings import Settings
from loom_v2.db.session import make_engine
from .repository import PoolRepository
from .client import ResourceClient
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import logging


def create_app(repository=None, *, settings=None):
    from loom_v2.observer.app import create_app as compose_api
    settings = settings or Settings()
    pool_settings = settings.model_copy(update={"service_name": "pool", "public_base_url": settings.pool_public_base_url or settings.public_base_url})
    engine = None
    if repository is None:
        engine = make_engine(settings.database_url)
        repository = PoolRepository(engine, settings=pool_settings)
        repository.resource_client = ResourceClient(settings.observer_url, settings)
    app = compose_api(repository, settings=pool_settings)
    app.title = "Loom Driver Pool"
    app.state.engine = engine
    original_lifespan = app.router.lifespan_context

    async def reconcile():
        while True:
            try:
                await repository.recover_execution_requests_for_driver(settings.workspace_id,
                    driver_id="", driver_instance_id="", driver_epoch=0)
                await repository.recover_persisted_activation_waits()
                for item in await repository.list_execution_requests(settings.workspace_id, states={"awaiting_auth"}):
                    if item.deadline_at and item.deadline_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
                        async with repository._execution_transaction(item.run_id):
                            run = await repository.get_run(item.run_id)
                            current = await repository.get_execution_request(item.workspace_id, item.request_id)
                            if (run.state == "awaiting_auth" and current.state == "awaiting_auth"
                                    and current.execution_id == run.execution_id
                                    and current.deadline_at is not None
                                    and current.deadline_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)):
                                await repository.fail_run(item.run_id, {"code": "execution_deadline_exceeded"})
            except Exception:
                logging.getLogger(__name__).exception("pool_recovery_failed")
            await asyncio.sleep(max(0.1, settings.agent_heartbeat_interval_seconds))

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            task = asyncio.create_task(reconcile())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                if getattr(repository, "resource_client", None) is not None:
                    await repository.resource_client.close()
    app.router.lifespan_context = lifespan
    return app


app = create_app()
