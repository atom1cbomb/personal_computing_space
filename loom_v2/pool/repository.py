from __future__ import annotations
import hmac
from sqlalchemy import select
from loom_v2.contracts.types import CapabilityHealthReport, ComputeBinding, TaskClosure, NodeInputBinding, ResourceRef
from loom_v2.db.models import RunRow
from loom_v2.contracts.refs import action_name

from loom_v2.observer.resources import ResourceRepository
from .tasks import TaskRepository


class PoolRepository(TaskRepository, ResourceRepository):
    """Task state authority; shares read-only resource projections with Observer."""

    async def init_db(self):
        await super().init_db()
        await self.recover_persisted_activation_waits()

    async def authorize_attempt(self, payload):
        """Check live queue ownership before a Slave starts a side effect."""
        owner = payload["execution_owner"]
        workspace = payload["workspace_id"]
        agents = await self.list_agents(workspace, "driver")
        identity = (payload["driver_id"], owner["instance_id"], payload["driver_epoch"])
        if not any((a["agent_id"], a["instance_id"], a["epoch"]) == identity and a["lease_state"] == "active" for a in agents):
            raise ValueError("stale_execution_owner")
        agent = await self._agent_record(workspace, "driver", identity[0], identity[1])
        if agent is None or not hmac.compare_digest(agent["lease_id_hash"], self._lease_hash(owner["lease_id"])):
            raise ValueError("stale_execution_owner")
        if self.sessions is None:
            matches = [r.run_id for r in self.runs.values() if r.execution_id == payload["execution_id"]]
        else:
            async with self._execution_read_session() as session:
                matches = list((await session.scalars(select(RunRow.run_id).where(RunRow.execution_id == payload["execution_id"]))).all())
        if len(matches) != 1:
            raise ValueError("stale_execution_id")
        async with self._execution_transaction(matches[0]):
            run = await self._assert_run_workspace(matches[0], workspace)
            claims = await self.list_execution_requests(workspace, run_id=run.run_id, states={"claimed"})
            if not any((i.driver_id, i.driver_instance_id, i.driver_epoch) == identity for i in claims):
                raise ValueError("stale_execution_owner")
            attempt = next((a for a in run.attempts if a["attempt_id"] == payload["attempt_id"]), None)
            if (run.state not in {"running", "cleaning_up"} or attempt is None
                    or payload["execution_epoch"] != run.execution_epoch
                    or attempt.get("target") != payload["target_slave"]):
                raise ValueError("stale_attempt")
            if attempt.get("node_id"):
                node = next(n for n in run.dynamic_nodes if n.node_id == attempt["node_id"])
                package = await self.get_capability_package(node.package_ref, run_id=run.run_id)
                export = package.export_for(node.action_descriptor_ref)
                if len(node.input_refs) == 1:
                    input_ref = node.input_refs[0]
                else:
                    values = [await self._load_json_content(ref) for ref in node.input_refs]
                    input_ref = await self.put_content({"inputs": values}, media_type="application/json")
                expected = TaskClosure(closure_id=node.node_id,
                    program={"action_ref": node.action_descriptor_ref.resource_id, "io_contract_ref": export.io_contract_ref},
                    node_input_bindings=[NodeInputBinding(node_id=node.action_descriptor_ref.resource_id, input_ref=input_ref)])
                expected_binding = ComputeBinding(binding_id=f"binding-{node.node_id}", hole_id=node.node_id,
                    action_descriptor_ref=node.action_descriptor_ref, capability_package_ref=node.package_ref,
                    target_resource_ref=ResourceRef(resource_id=self.names.name("slaves", payload["target_slave"])),
                    instance_name=node.instance_name)
            else:
                expected = run.committed.snapshot
                expected_binding = expected.compute_bindings[0] if expected.compute_bindings else None
            supplied = TaskClosure.model_validate(payload["closure"])
            binding = ComputeBinding.model_validate(payload["binding"]) if payload.get("binding") else None
            if (supplied.model_dump(mode="json") != expected.model_dump(mode="json")
                    or payload["action"] != action_name(expected.program.action_ref or expected.compute.action_ref)
                    or (binding.model_dump(exclude={"expected_activation_revision"}) if binding else None)
                    != (expected_binding.model_dump(exclude={"expected_activation_revision"}) if expected_binding else None)
                    or (binding is not None and (binding.expected_activation_revision is None
                        or (expected_binding.expected_activation_revision is not None
                            and binding.expected_activation_revision != expected_binding.expected_activation_revision)))):
                raise ValueError("attempt_contract_mismatch")
        return {"authorized": True}

    async def register_resource(self, *args, **kwargs):
        if getattr(self, "resource_client", None) is not None:
            return await self.resource_client.register_resource(*args, **kwargs)
        return await super().register_resource(*args, **kwargs)

    async def update_resource_attributes(self, *args, **kwargs):
        if getattr(self, "resource_client", None) is not None:
            return await self.resource_client.update_resource_attributes(*args, **kwargs)
        return await super().update_resource_attributes(*args, **kwargs)

    async def register_workspace_package(self, *args, **kwargs):
        if getattr(self, "resource_client", None) is not None:
            return await self.resource_client.register_workspace_package(*args, **kwargs)
        return await super().register_workspace_package(*args, **kwargs)

    async def promote_capability_package(self, *args, **kwargs):
        if getattr(self, "resource_client", None) is not None:
            return await self.resource_client.promote_capability_package(*args, **kwargs)
        return await super().promote_capability_package(*args, **kwargs)

    async def set_capability_desired(self, package_ref, *args, **kwargs):
        package = await self.get_capability_package(package_ref, run_id=kwargs.get("run_id"))
        if package.scope == "workspace_reusable" and getattr(self, "resource_client", None) is not None:
            result = await self.resource_client.set_capability_desired(package_ref, *args, **kwargs)
            await self._load_workspace_packages()
            return result
        return await super().set_capability_desired(package_ref, *args, **kwargs)

    async def record_capability_health(self, report, **kwargs):
        if not isinstance(report, CapabilityHealthReport):
            report = CapabilityHealthReport.model_validate(report)
        package = await self.get_capability_package(report.package_version_ref, run_id=kwargs.get("run_id"))
        if package.scope == "workspace_reusable" and getattr(self, "resource_client", None) is not None:
            result = await self.resource_client.record_capability_health(report, **kwargs)
            await self._load_workspace_packages()
        else:
            result = await super().record_capability_health(report, **kwargs)
        if result.activation_state == "ready":
            await self.recover_activation_wait(result)
        return result
