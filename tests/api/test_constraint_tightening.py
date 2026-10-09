import pytest

from loom_v2.observer.repository import ObserverRepository


@pytest.mark.asyncio
async def test_relaxed_constraint_patch_is_rejected_without_polluting_draft():
    repo = ObserverRepository()
    run = await repo.open_run("run-constraint", "task-constraint", "run test code")
    first = await repo.apply_patch(
        run.run_id,
        run.draft_version,
        run.draft_digest,
        "constraint-1",
        [{"kind": "add_constraint", "value": {"subject": ["ComputeSpec"], "predicate": {"op": "le", "field": "cpu_seconds", "value": 60}, "source": "Requester", "fate": "preserve"}}],
    )
    with pytest.raises(ValueError, match="constraint_not_monotonic"):
        await repo.apply_patch(
            run.run_id,
            first.draft_version,
            first.draft_digest,
            "constraint-2",
            [{"kind": "tighten_constraint", "value": {"parent": {"op": "le", "field": "cpu_seconds", "value": 60}, "child": {"op": "le", "field": "cpu_seconds", "value": 90}}}],
        )
    state = await repo.get_run(run.run_id)
    assert state.draft_version == first.draft_version
