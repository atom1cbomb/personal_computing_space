import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

from loom_v2.observer.app import create_app
from loom_v2.testing.observer import create_embedded_app


class DriverTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"conversation_ref": "conversation-1", "status": "completed"})


def test_message_gateway_is_removed_in_favor_of_external_submissions():
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/messages", json={"request_id": "req-1", "conversation_ref": "conversation-1", "text": "hello"})
    assert response.status_code == 404


class BlockingDriverTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.released = asyncio.Event()
        self.requests = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        await self.released.wait()
        return httpx.Response(200, json={"status": "completed"})


def test_submission_endpoint_is_the_only_public_task_entrypoint():
    with TestClient(create_embedded_app()) as client:
        response = client.post("/api/v1/messages", json={"request_id": "req-dup", "conversation_ref": "conversation-dup", "text": "hello"})
    assert response.status_code == 404
