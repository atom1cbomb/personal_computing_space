import pytest

from loom_business_skills import Problem
import loom_business_skills.memory as implementation
from conftest import draft


@pytest.mark.parametrize("status", ["awaiting_auth", "awaiting_decision", "poll_timeout", "failed"])
async def test_unfinished_task_keeps_identity_and_cannot_be_accepted(memory, client, store, monkeypatch, status):
    await memory.learn(draft(client))
    run = client.names.name("runs", "server-owned-run")
    async def execute(*args, **kwargs):
        return {"status": status, "interaction": "task", "receipt": {"run": run},
                "run": {"state": status, "private": "DO-NOT-STORE-HISTORY"}}
    monkeypatch.setattr(implementation, "execute_action", execute)
    execution, _ = await memory.run("paper-review", "catalog", {}, idempotency_key="task")
    assert execution["run"] == run and execution["status"] == status
    assert store.get("paper-review")["evidence"]["catalog"]["level"] == "documented"
    assert b"DO-NOT-STORE-HISTORY" not in store.path.read_bytes()
    with pytest.raises(Problem, match="verified_execution_required"):
        await memory.accept("paper-review", "catalog", execution["id"])


async def test_provider_exception_does_not_expose_secret_or_mark_success(memory, client, store, monkeypatch):
    await memory.learn(draft(client))
    async def execute(*args, **kwargs):
        raise ValueError("upstream rejected Bearer very-private-token-material")
    monkeypatch.setattr(implementation, "execute_action", execute)
    with pytest.raises(Problem, match="step_call_failed") as caught:
        await memory.run("paper-review", "catalog", {}, idempotency_key="task")
    assert "very-private" not in str(caught.value.details)
    assert b"very-private" not in store.path.read_bytes()
    assert store.get("paper-review")["evidence"]["catalog"]["last_execution"]["status"] == "failed_or_unknown"
