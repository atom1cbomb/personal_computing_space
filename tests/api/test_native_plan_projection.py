"""Native task projections read durable state without loading Run histories."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.contracts.types import ClosureContract
from loom_v2.observer.repository import ObserverRepository


@pytest.fixture(params=["memory", "sqlite"])
async def repositories(request, tmp_path):
    engine = None
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'plans.db'}")
    writer = ObserverRepository(engine)
    await writer.init_db()
    reader = ObserverRepository(engine) if engine is not None else writer
    yield writer, reader, engine
    if engine is not None:
        await engine.dispose()


def plan(closure_id="plan", *, workspace_id="workspace-default"):
    return ClosureContract(
        closure_id=closure_id, goal="execute plan", workspace_id=workspace_id,
        body={"closure_id": closure_id, "metadata": {"input": {"value": 1}}},
    )


@contextmanager
def queries(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    if engine is not None:
        event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        if engine is not None:
            event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_native_projections_filter_workspace_and_target_without_loading_histories(repositories, monkeypatch):
    writer, reader, engine = repositories
    first = await writer.submit_run_plan(workspace_id="workspace-default", request_id="first", plan=plan())
    second = await writer.submit_run_plan(workspace_id="workspace-default", request_id="second", plan=plan("other"))
    foreign = await writer.submit_run_plan(workspace_id="foreign", request_id="foreign", plan=plan("foreign", workspace_id="foreign"))
    opened = await writer.open_run("opened", "opened", "opened")
    # A later queue request for the same committed version is still one plan.
    first.state = "settled"
    await writer._persist_execution_request(first)
    await writer.enqueue_committed_run(
        workspace_id="workspace-default", request_id="requeue", run_id=first.run_id,
        closure_version=first.closure_version,
    )

    async def no_load(*args, **kwargs):
        pytest.fail("native projections must not materialize Run or request histories")

    for name in ("_all_records", "_load", "list_execution_requests", "_load_execution_request"):
        monkeypatch.setattr(reader, name, no_load)
    with queries(engine) as sql:
        summaries = await reader.list_plan_summaries("workspace-default")
        targeted = await reader.list_plan_summaries("workspace-default", run_id=first.run_id, closure_version=first.closure_version)
        by_version = await reader.list_plan_summaries("workspace-default", closure_version=second.closure_version)
        runs = await reader.list_run_summaries("workspace-default", run_id=first.run_id)
        empty = await reader.list_run_summaries("workspace-default", run_id=opened.run_id)
        assert await reader.list_plan_summaries("workspace-default", run_id=foreign.run_id) == []
        assert await reader.list_plan_summaries("foreign", run_id=first.run_id) == []
        assert await reader.list_plan_summaries("workspace-default", closure_version="missing") == []
        assert await reader.list_run_summaries("workspace-default", run_id=foreign.run_id) == []
    assert {(item.run_id, item.closure_version, item.snapshot_digest) for item in summaries} == {
        (first.run_id, first.closure_version, first.snapshot_digest),
        (second.run_id, second.closure_version, second.snapshot_digest),
    }
    assert len(summaries) == 2
    assert len(targeted) == len(by_version) == len(runs) == len(empty) == 1
    assert targeted[0].run_id == runs[0].run_id == first.run_id
    assert by_version[0].run_id == second.run_id
    assert runs[0].closure_version == first.closure_version
    assert runs[0].snapshot_digest == first.snapshot_digest
    assert runs[0].execution_id is None
    assert runs[0].execution_epoch == 1
    assert empty[0].closure_version is empty[0].snapshot_digest is None
    for statement in sql:
        assert all(column not in statement for column in (
            "runs.events", "runs.draft", "runs.outcome", "run_execution_requests.request_payload",
            "run_execution_requests.closure_contract",
        ))


async def test_run_result_reads_fresh_execution_identity_and_isolates_workspace(repositories, monkeypatch):
    writer, reader, engine = repositories
    item = await writer.submit_run_plan(workspace_id="workspace-default", request_id="first", plan=plan())
    assert (await reader.read_run_result("workspace-default", item.run_id))["state"] == "committed"
    record = await writer.get_run(item.run_id)
    record.state = "completed"
    record.outcome = {"answer": {"value": 42}}
    record.execution_id = "execution-current"
    record.execution_epoch = 3
    await writer._persist(record)

    async def no_load(*args, **kwargs):
        pytest.fail("Run reads must not materialize execution history")

    monkeypatch.setattr(reader, "_load", no_load)
    monkeypatch.setattr(reader, "_all_records", no_load)
    with queries(engine) as sql:
        result = await reader.read_run_result("workspace-default", item.run_id)
        summary, = await reader.list_run_summaries("workspace-default", run_id=item.run_id)
    assert result == {
        "state": "completed", "outcome": {"answer": {"value": 42}}, "run_id": item.run_id,
        "closure_version": item.closure_version, "snapshot_digest": item.snapshot_digest,
        "execution_id": "execution-current", "execution_epoch": 3,
    }
    assert (summary.state, summary.execution_id, summary.execution_epoch) == ("completed", "execution-current", 3)
    result["outcome"]["answer"]["value"] = 0
    assert (await reader.read_run_result("workspace-default", item.run_id))["outcome"] == {"answer": {"value": 42}}
    for workspace, run_id in (("foreign", item.run_id), ("workspace-default", "missing")):
        with pytest.raises(KeyError):
            await reader.read_run_result(workspace, run_id)
    assert all("runs.events" not in statement and "runs.draft" not in statement for statement in sql)


async def test_frozen_plans_preserve_committed_body_and_original_idempotency_payload(repositories, monkeypatch):
    writer, reader, engine = repositories
    original_commit = writer.commit

    async def commit_refined_body(run_id, version_id, digest):
        # Exercise a committed body that differs from the input contract body.
        record = await writer._load(run_id)
        snapshot = record.draft.snapshot.model_copy(deep=True)
        snapshot.metadata["refinement"] = {"version": version_id}
        record.draft = record.draft.model_copy(update={"snapshot": snapshot, "snapshot_digest": snapshot.canonical_digest()})
        return await original_commit(run_id, version_id, record.draft_digest)

    monkeypatch.setattr(writer, "commit", commit_refined_body)
    original = plan(workspace_id="")
    first = await writer.submit_run_plan(
        workspace_id="workspace-default", request_id="first", plan=original, conversation_ref="conversation",
    )
    record = await writer.get_run(first.run_id)
    initial = deepcopy(first.closure_contract)
    assert initial["body"] == record.committed.snapshot.model_dump(mode="json")
    assert initial["workspace_id"] == "workspace-default"
    assert initial["origin_conversation_ref"] == "conversation"
    assert first.request_payload["plan"] == original.model_dump(mode="json")
    retried = await writer.submit_run_plan(
        workspace_id="workspace-default", request_id="first", plan=original, conversation_ref="conversation",
    )
    assert retried.run_id == first.run_id

    record.state = "awaiting_decision"
    record.outcome = {"decision": "repair"}
    await writer._persist(record)
    await writer._settle_execution_request(record.run_id, outcome=record.outcome)
    repaired_input = original.model_copy(deep=True)
    repaired_input.body.metadata["input"]["value"] = 2
    repaired = await writer.resubmit_run_plan(
        workspace_id="workspace-default", run_id=first.run_id, request_id="repaired",
        base_closure_version=first.closure_version, plan=repaired_input,
    )
    record = await writer.get_run(first.run_id)
    latest = deepcopy(repaired.closure_contract)
    assert latest["body"] == record.committed.snapshot.model_dump(mode="json")
    assert repaired.request_payload["plan"] == repaired_input.model_dump(mode="json")
    retry = await writer.resubmit_run_plan(
        workspace_id="workspace-default", run_id=first.run_id, request_id="repaired",
        base_closure_version=first.closure_version, plan=repaired_input,
    )
    assert retry.closure_version == repaired.closure_version
    record.closure_contract.body.metadata["input"]["value"] = 99
    record.committed.snapshot.metadata["input"]["value"] = 99
    record.draft.snapshot.metadata["input"]["value"] = 99
    await writer._persist(record)

    async def no_load(*args, **kwargs):
        pytest.fail("frozen plan reads must use the stored execution request body")

    for name in ("_load", "_all_records", "_load_execution_request", "list_execution_requests"):
        monkeypatch.setattr(reader, name, no_load)
    with queries(engine) as sql:
        before = await reader.read_frozen_plan("workspace-default", first.run_id, first.closure_version)
        after = await reader.read_frozen_plan("workspace-default", first.run_id, repaired.closure_version)
    assert before == initial
    assert after == latest
    before["body"]["metadata"]["input"]["value"] = 0
    assert await reader.read_frozen_plan("workspace-default", first.run_id, first.closure_version) == initial
    summaries = await reader.list_plan_summaries("workspace-default", run_id=first.run_id)
    assert {item.closure_version for item in summaries} == {first.closure_version, repaired.closure_version}
    for workspace, run_id, version in (
        ("foreign", first.run_id, first.closure_version),
        ("workspace-default", "missing", first.closure_version),
        ("workspace-default", first.run_id, "missing"),
    ):
        with pytest.raises(KeyError):
            await reader.read_frozen_plan(workspace, run_id, version)
    assert all("FROM runs" not in statement and "request_payload" not in statement for statement in sql)


async def test_queue_entries_without_a_frozen_plan_do_not_shadow_plan_resources(repositories):
    from dataclasses import replace

    writer, reader, engine = repositories
    item = await writer.submit_run_plan(workspace_id="workspace-default", request_id="submitted", plan=plan())
    incomplete = replace(item, request_id="cleanup-old", state="settled", snapshot_digest="", closure_contract={})
    await writer._persist_execution_request(incomplete)
    summaries = await reader.list_plan_summaries("workspace-default")
    assert len(summaries) == 1
    assert summaries[0].snapshot_digest == item.snapshot_digest
    assert await reader.read_frozen_plan("workspace-default", item.run_id, item.closure_version) == item.closure_contract
    orphan = replace(incomplete, request_id="cleanup-only", closure_version="cleanup-without-plan")
    await writer._persist_execution_request(orphan)
    assert await reader.list_plan_summaries("workspace-default", closure_version=orphan.closure_version) == []
    with pytest.raises(KeyError):
        await reader.read_frozen_plan("workspace-default", orphan.run_id, orphan.closure_version)
