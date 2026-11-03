from __future__ import annotations

import httpx
import pytest

from loom_orchestration import GripError, LoomClient, Names
from loom_orchestration.grip import Reply
from loom_v2.observer.app import create_app
from loom_v2.observer.repository import ObserverRepository
from loom_v2.contracts.errors import DomainError
from loom_v2.pool.client import ResourceClient
from loom_v2.pool.repository import PoolRepository
from loom_v2.settings import Settings


@pytest.fixture
async def directory():
    settings = Settings(grip_token="delete-test", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    app = create_app(repo, settings=settings)
    async with LoomClient(
        "http://observer", token=settings.grip_token, allow_insecure_grip=True,
        transport=httpx.ASGITransport(app=app),
    ) as client:
        yield client, repo, app
    await app.state.service_access.http.aclose()
    await app.state.gateway.close()
    await repo.tasks.close()


async def register(client, name, **fields):
    resource = client.names.name("resources", name)
    await client.invoke(client.names.root, client.names.action("register-resource"), {
        "resource_id": resource, "mcue_ref": client.names.mcue("computing/vm"), **fields,
    })
    return resource


async def delete(client, resource, **fields):
    return await client.invoke(client.names.root, client.names.action("delete-resource"), {
        "resource_id": resource, **fields,
    })


@pytest.mark.parametrize("state", ["unknown", "running", "stopped", "failed"])
async def test_soft_delete_refuses_existing_entity_and_directs_caller_to_manager(directory, state):
    client, repo, app = directory
    manager = await register(client, "manager")
    target = await register(client, "vm", state=state, managed_by=manager,
                            attributes={"display_name": "Keep until deleted"})
    before = await client.describe(target)
    with pytest.raises(GripError, match="resource_not_deleted") as caught:
        await delete(client, target)
    assert caught.value.remote
    assert "Resource has not been deleted" in caught.value.message
    assert "Ask the resource manager" in caught.value.message
    assert manager in caught.value.message
    assert await client.describe(target) == before
    assert not repo.runs


async def test_refusal_without_manager_is_an_actionable_grip_error(directory):
    client, repo, app = directory
    target = await register(client, "unmanaged", state="running")
    message = client._grip_request("INVOKE", client.names.root,
        action=client.names.action("delete-resource"), input={"resource_id": target})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as raw:
        response = await raw.post("http://observer/.well-known/grip", json=message)
    assert response.status_code == 200
    reply = Reply.model_validate(response.json())
    assert reply.kind == "error" and reply.error_code == "resource_not_deleted"
    assert "resource manager" in reply.error_message
    assert (await repo.get_resource(repo.settings.workspace_id, target)).state == "running"


async def test_soft_delete_after_caller_reports_entity_deleted(directory):
    client, repo, app = directory
    target = await register(client, "vm", state="running")
    with pytest.raises(GripError, match="resource_not_deleted"):
        await delete(client, target)
    # The caller asks the manager to remove the entity and reports that result.
    assert await register(client, "vm", state="deleted") == target
    assert await delete(client, target, force=False) == {"resource": target}
    assert await client.resources(mcue="computing/vm") == []
    with pytest.raises(GripError, match="not_found"):
        await client.describe(target)
    with pytest.raises(GripError, match="not_found"):
        await delete(client, target)


@pytest.mark.parametrize("force", [False, True])
async def test_delete_parent_leaves_child_with_its_original_manager(directory, force):
    client, repo, app = directory
    parent = await register(client, "parent", state="running" if force else "deleted")
    child = await register(client, "child", managed_by=parent, state="running")
    child_before = await client.describe(child)
    assert await delete(client, parent, force=force) == {"resource": parent}
    assert await client.describe(child) == child_before
    assert [r["uri"] for r in await client.resources(mcue="computing/vm")] == [child]
    # Force deletion still works when the recorded manager no longer exists.
    assert await delete(client, child, force=True) == {"resource": child}


async def test_delete_action_has_discoverable_strict_schemas(directory):
    client, repo, app = directory
    description = await client.describe(client.names.root)
    offer = next(o for o in description["offers"]
                 if o["action"]["uri"] == client.names.action("delete-resource"))
    schema = await client.invoke(offer["action"]["input_schema"], client.names.action("read"), {})
    assert schema["properties"]["force"]["default"] is False
    output = await client.invoke(offer["action"]["output_schema"], client.names.action("read"), {})
    assert output["required"] == ["resource"]
    target = await register(client, "vm")
    for invalid in ({"resource_id": target, "force": "false"},
                    {"resource_id": target, "force": 1},
                    {"resource_id": target, "unexpected": True}, {},
                    {"resource_id": "invalid"}):
        with pytest.raises(GripError, match="invalid_input"):
            await client.invoke(client.names.root, client.names.action("delete-resource"), invalid)
    assert await delete(client, target, force=True) == {"resource": target}


async def test_delete_cannot_remove_other_workspaces_or_system_resources(directory):
    client, repo, app = directory
    other = Names(workspace="other")
    target = other.name("resources", "vm")
    await repo.register_resource("other", {
        "resource_ref": {"resource_id": target}, "mcue_ref": other.mcue("computing/vm"),
    })
    for resource in (target, client.names.root, client.names.mcue("computing/vm"),
                     client.names.name("resources", "missing")):
        with pytest.raises(GripError, match="not_found"):
            await delete(client, resource, force=True)
    assert (await repo.get_resource("other", target)).resource_ref.resource_id == target


async def test_pool_delegates_record_deletion_to_observer_authority(directory):
    client, repo, app = directory
    target = await register(client, "vm", state="running")
    pool = PoolRepository(settings=repo.settings)
    pool.resource_client = ResourceClient(
        "http://observer", repo.settings, transport=httpx.ASGITransport(app=app),
    )
    try:
        with pytest.raises(DomainError, match="resource_not_deleted") as caught:
            await pool.delete_resource(repo.settings.workspace_id, target)
        assert caught.value.envelope.details["resource"] == target
        await pool.delete_resource(repo.settings.workspace_id, target, force=True)
        assert await client.resources(mcue="computing/vm") == []
    finally:
        await pool.resource_client.close()
