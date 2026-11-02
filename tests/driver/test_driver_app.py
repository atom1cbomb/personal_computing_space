import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from loom_v2.contracts.agents import AgentLease
from loom_v2.driver.app import create_app
from loom_v2.settings import Settings


class ObserverTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"agent_id": "driver-default", "instance_id": "test-instance", "workspace_id": "workspace-default", "lease_id": "lease-1", "epoch": 1, "heartbeat_interval_seconds": 60})
        return httpx.Response(200, json={"agent_id": "driver-default", "instance_id": "test-instance", "workspace_id": "workspace-default", "lease_id": "lease-1", "epoch": 1, "heartbeat_interval_seconds": 60})


def test_driver_message_endpoint_is_removed(monkeypatch):
    monkeypatch.setenv("LOOM_INTERNAL_API_SECRET", "secret")
    app = create_app(observer_transport=ObserverTransport())
    with TestClient(app) as client:
        response = client.post("/driver/v1/messages", json={"request_id": "r", "conversation_ref": "c", "text": "hello"})
    assert response.status_code == 404


def test_driver_registers_on_startup():
    transport = ObserverTransport()
    app = create_app(observer_transport=transport)
    with TestClient(app):
        assert transport.requests[0].url.path == "/internal/v1/agents/register"


@pytest.mark.parametrize("failed_step", ["register", "execution.recover"])
async def test_lease_recovery_retries_until_execution_polling_resumes(failed_step):
    class Control:
        driver_id = "driver-default"
        instance_id = "same-driver"
        workspace_id = "workspace-default"
        driver_epoch = None

        def __init__(self):
            self.registrations = 0
            self.heartbeats = 0
            self.recovered = set()
            self.resumed = asyncio.Event()

        async def register(self, registration=None):
            self.registrations += 1
            if self.registrations == 2 and failed_step == "register":
                raise RuntimeError("temporary_registration_failure")
            self.driver_epoch = self.registrations
            return AgentLease(agent_id=self.driver_id, instance_id=self.instance_id,
                              workspace_id=self.workspace_id, lease_id=f"lease-{self.driver_epoch}",
                              epoch=self.driver_epoch, heartbeat_interval_seconds=0.1)

        async def heartbeat(self):
            self.heartbeats += 1
            if self.heartbeats == 1:
                raise RuntimeError("stale_driver_epoch")

        async def command(self, command, arguments, **kwargs):
            if command == "execution.recover":
                if self.registrations == 2 and failed_step == command:
                    raise RuntimeError("temporary_recovery_failure")
                self.recovered.add(self.driver_epoch)
            elif command == "execution.claim_next":
                assert self.driver_epoch in self.recovered
                if self.registrations >= 3:
                    self.resumed.set()
            return {}

        async def release(self):
            pass

        async def close(self):
            pass

    control = Control()
    app = create_app(control_client=control, settings=Settings(agent_registration_retry_seconds=0.1))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(control.resumed.wait(), timeout=3)
        assert app.state.lease.epoch == 3
        assert app.state.service.execution_loop_active
        assert not app.state.execution_task.done()
        assert control.recovered == {1, 3}

