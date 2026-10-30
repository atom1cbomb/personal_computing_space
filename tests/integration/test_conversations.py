from tests.support.grip_client import submit_plan, submission, run_action
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from loom_v2.testing.observer import create_embedded_app as create_app
from loom_v2.observer.repository import ObserverRepository


@pytest.mark.asyncio
async def test_message_events_survive_repository_reload():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    repo = ObserverRepository(engine)
    await repo.init_db()
    run = await repo.open_run("run-message", "conversation-1", "能力问题")
    await repo.append_message(run.run_id, "user", "能力问题")
    await repo.append_message(run.run_id, "assistant", "两者能力相同")

    restored = ObserverRepository(engine)
    await restored.init_db()
    conversation = await restored.get_conversation("conversation-1")

    assert [message["content"] for message in conversation["messages"]] == ["能力问题", "两者能力相同"]


@pytest.mark.asyncio
async def test_persisted_conversation_history_is_ordered_by_run_creation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    repo = ObserverRepository(engine)
    await repo.init_db()
    first = await repo.open_run("run-z-first", "conversation-ordered", "第一问")
    await repo.append_message(first.run_id, "user", "第一问")
    second = await repo.open_run("run-a-second", "conversation-ordered", "第二问")
    await repo.append_message(second.run_id, "user", "第二问")

    restored = ObserverRepository(engine)
    await restored.init_db()
    summary = (await restored.list_conversations())[0]
    history = await restored.get_conversation("conversation-ordered")

    assert summary["latest_run_id"] == "run-a-second"
    assert [message["content"] for message in history["messages"]] == ["第一问", "第二问"]


def test_run_history_exposes_persisted_messages():
    repository = ObserverRepository()
    client = TestClient(create_app(repository))
    submitted = submit_plan(client, **{
            "idempotency_key": "conversation-history-request",
            "conversation_ref": "conversation-history",
            "plan": {"closure_id": "conversation-history-closure", "goal": "能力问题", "body": {"closure_id": "conversation-history-closure"}},
        },
    )
    assert submitted["kind"] == "reply"
    run = submission(client, "conversation-history-request")
    asyncio.run(repository.append_message(run["run_id"], "user", "能力问题"))
    asyncio.run(repository.append_message(run["run_id"], "assistant", "两者能力相同"))

    history = run_action(client, run["run_id"], input={"include_history": True})
    messages = [event for event in history["events"] if event.get("phase") == "message"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert [message["content"] for message in messages] == ["能力问题", "两者能力相同"]
