from __future__ import annotations
from loom_v2.contracts.types import CapabilityHealthReport

from .resources import ResourceRepository


class ObserverRepository(ResourceRepository):
    """Resource authority; task commands are delegated to the Driver Pool."""

    def __init__(self, *args, task_client=None, **kwargs):
        super().__init__(*args, **kwargs)
        from loom_v2.pool.client import PoolClient
        self.tasks = task_client or PoolClient(self.settings.pool_url, self.settings)

    def __getattr__(self, name):
        if name in self.tasks.methods:
            return getattr(self.tasks, name)
        raise AttributeError(name)

    async def promote_capability_package(self, *args, **kwargs):
        return await self.tasks.promote_capability_package(*args, **kwargs)

    async def abandon_capability_package(self, package_ref, **kwargs):
        package = await self.get_capability_package(package_ref)
        if package.workspace_id is None:
            return await self.tasks.abandon_capability_package(package_ref, **kwargs)
        return await super().abandon_capability_package(package_ref, **kwargs)

    async def set_capability_desired(self, package_ref, *args, **kwargs):
        package = await self.get_capability_package(package_ref, run_id=kwargs.get("run_id"))
        if package.workspace_id is None:
            return await self.tasks.set_capability_desired(package_ref, *args, **kwargs)
        return await super().set_capability_desired(package_ref, *args, **kwargs)

    async def record_capability_health(self, report, **kwargs):
        if not isinstance(report, CapabilityHealthReport):
            report = CapabilityHealthReport.model_validate(report)
        package = await self.get_capability_package(report.package_version_ref, run_id=kwargs.get("run_id"))
        if package.workspace_id is None:
            return await self.tasks.record_capability_health(report, **kwargs)
        return await super().record_capability_health(report, **kwargs)
