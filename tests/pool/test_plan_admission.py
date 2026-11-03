"""Complete plans use atomic admission and bounded control-plane work."""
import pytest
from sqlalchemy import event, select, func, update

from loom_v2.contracts.agents import AgentRegistration
from loom_v2.contracts.types import ClosureContract, ClosureVersion, TaskClosure
from loom_v2.db.models import RunRow, RunEventRow, RunCapabilityStateRow, RunExecutionRequestRow, WorkspaceCapabilityPackageRow
from tests.pool.test_concurrency import pools, postgres_container, submit, register, claim
from tests.db.test_health_history_isolation import service

pytestmark = pytest.mark.integration


async def test_complete_plan_is_inserted_frozen_once_and_claim_checked_once(pools, monkeypatch):
    repo, reader = pools
    lease = await register(repo, "admission-driver")
    sql = []
    def capture(conn, cursor, statement, params, context, many):
        sql.append(statement.lower())
    event.listen(repo.engine.sync_engine, "before_cursor_execute", capture)
    try:
        item = await submit(repo, "direct-admission")
    finally:
        event.remove(repo.engine.sync_engine, "before_cursor_execute", capture)
    assert sum(s.startswith("insert into runs ") for s in sql) == 1
    assert not any(s.startswith("update runs ") for s in sql)
    record = await reader.get_run(item.run_id)
    assert record.state == "committed" and record.committed.snapshot_digest == item.snapshot_digest
    assert not any(e.get("to_state") == "thinking" or e.get("event") == "refinement_started" for e in record.events)
    checks = 0
    original = repo._evaluate_readiness
    async def count_checks(*args, **kwargs):
        nonlocal checks
        checks += 1
        return await original(*args, **kwargs)
    monkeypatch.setattr(repo, "_evaluate_readiness", count_checks)
    result = await claim(repo, lease)
    assert result["run"]["state"] == "running" and checks == 1


async def test_admission_rolls_back_run_index_events_and_queue_together(pools, monkeypatch):
    repo, reader = pools
    async def fail_queue(item):
        raise RuntimeError("queue_write_failed")
    monkeypatch.setattr(repo, "_persist_execution_request", fail_queue)
    with pytest.raises(RuntimeError, match="queue_write_failed"):
        await submit(repo, "admission-rollback")
    async with reader.sessions() as session:
        for model in (RunRow, RunEventRow, RunCapabilityStateRow, RunExecutionRequestRow):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert repo.runs == {} and repo._run_capabilities == {}


async def test_readiness_sees_new_private_candidates_before_persisting_the_patch(pools):
    from tests.api.test_dynamic_nodes import _dynamic_run, _register_slave
    repo, _ = pools
    await _register_slave(repo, "slave-a", "private-candidate-slave")
    run, patched = await _dynamic_run(repo)
    assert patched.readiness["ready"], patched.readiness


async def test_state_update_skips_plan_serialization_and_preserves_other_columns(pools, monkeypatch):
    repo, reader = pools
    item = await submit(repo, "partial-state")
    await repo.cancel_run(item.run_id)
    async with reader.sessions.begin() as session:
        await session.execute(update(RunRow).where(RunRow.run_id == item.run_id).values(goal="independent change"))
    def no_dump(*args, **kwargs):
        raise AssertionError("state changes must not serialize the plan")
    monkeypatch.setattr(ClosureVersion, "model_dump", no_dump)
    sql = []
    def capture(conn, cursor, statement, params, context, many):
        sql.append(statement.lower())
    event.listen(repo.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await repo.close_run(item.run_id)
    finally:
        event.remove(repo.engine.sync_engine, "before_cursor_execute", capture)
    queries = [s for s in sql if "from runs" in s or s.startswith("update runs ")]
    assert queries and any(s.startswith("update runs ") for s in queries)
    assert not any(column in s for s in queries for column in ("draft", "committed", "closure_contract", "outcome", "attempts", "capability_packages", "dynamic_nodes"))
    async with reader.sessions() as session:
        row = (await session.execute(select(RunRow.state, RunRow.goal).where(RunRow.run_id == item.run_id))).one()
    assert tuple(row) == ("closed", "independent change")


async def test_readiness_reads_only_referenced_packages_and_refreshes_changed_target(pools):
    repo, cold = pools
    package = await service(repo, "readiness-target")
    unrelated = await service(repo, "readiness-unrelated")
    await repo.register_agent(AgentRegistration(role="slave", agent_id="slave-a", instance_id="ready-slave",
        workspace_id="workspace-default", endpoint_url="http://slave-a", protocol_version="loom.v1",
        capabilities={"executor_descriptors": [{"package_type": "service", "kind": "container:http", "version": "1"}]}))
    async with repo.sessions.begin() as session:
        row = await session.get(WorkspaceCapabilityPackageRow, ("workspace-default", unrelated.version_ref))
        row.package = {"malformed": True}
    export = package.action_exports[0]
    plan = ClosureContract(closure_id="targeted", goal="targeted readiness", body=TaskClosure(
        closure_id="targeted", program={"action_ref": export.action_descriptor_ref.resource_id, "io_contract_ref": export.io_contract_ref},
        compute={"typed_holes": [{"hole_id": "target", "status": "bound", "binding_ref": "binding"}]},
        compute_bindings=[{"binding_id": "binding", "hole_id": "target", "bound_by": "agent",
            "action_descriptor_ref": export.action_descriptor_ref,
            "capability_package_ref": {"resource_id": package.version_ref, "version_or_digest": package.package_digest},
            "target_resource_ref": {"resource_id": repo.names.name("slaves", "slave-a")}}]))
    queries = []
    def capture(conn, cursor, sql, params, context, many):
        if "from workspace_capability_packages" in sql.lower():
            queries.append(sql.lower())
            assert "workspace_capability_packages.package_ref =" in sql.lower()
    event.listen(repo.engine.sync_engine, "before_cursor_execute", capture)
    try:
        item = await cold.submit_run_plan(workspace_id="workspace-default", request_id="targeted", plan=plan)
        assert queries
        # A warm validation cache must not hide a package revoked while queued.
        package.publication_state = "abandoned"
        await repo._persist_workspace_package(package)
        lease = await register(cold, "targeted-driver")
        claimed = await claim(cold, lease)
        assert claimed["run"]["run_id"] == item.run_id
        assert claimed["run"]["state"] == "awaiting_decision"
    finally:
        event.remove(repo.engine.sync_engine, "before_cursor_execute", capture)
