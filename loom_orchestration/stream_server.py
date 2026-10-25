"""ASGI side of the GRIP stream binding, shared by gateways and relays."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from contextlib import suppress
from typing import Any

from pydantic import ValidationError

from .bindings import timestamp
from .grip import GripError, REQUEST_ID, Request, decode
from .streams import MAX_MESSAGE, STREAM_PROTOCOL, StreamAborted, WebSocketStream, bridge_streams


@dataclass
class OpenedStream:
    stream: Any
    body: dict
    expires_at: float | None = None


class ASGIConnection:
    def __init__(self, websocket):
        self.websocket = websocket

    async def recv(self):
        message = await self.websocket.receive()
        if message["type"] == "websocket.disconnect":
            code = message.get("code", 1006)
            if code == 1000:
                return None
            raise StreamAborted(code)
        return message.get("bytes") if message.get("bytes") is not None else message.get("text")

    async def send(self, message):
        if isinstance(message, bytes):
            await self.websocket.send_bytes(message)
        else:
            await self.websocket.send_text(message)

    async def close(self, code=1000):
        await self.websocket.close(code=code)


async def serve_stream(websocket, opener, *, handshake_timeout=30):
    """opener validates authorization and opens exactly one upstream channel."""
    if STREAM_PROTOCOL not in websocket.scope.get("subprotocols", []):
        await websocket.close(code=1002)
        return
    await websocket.accept(subprotocol=STREAM_PROTOCOL)
    connection = ASGIConnection(websocket)
    handshake_end = time.monotonic() + handshake_timeout
    request_id = None
    opened = None
    confirmed = False
    opening = early = None
    try:
        raw = await asyncio.wait_for(connection.recv(), handshake_timeout)
        if not isinstance(raw, str) or len(raw.encode()) > MAX_MESSAGE:
            raise GripError("invalid_message")
        data = decode(raw.encode())
        if isinstance(data, dict) and isinstance(data.get("request_id"), str) and REQUEST_ID.fullmatch(data["request_id"]):
            request_id = data["request_id"]
        request = Request.model_validate(data)
        if request.method != "INVOKE":
            raise GripError("invalid_message")
        if STREAM_PROTOCOL not in request.accepted_protocols:
            raise GripError("unsupported_binding")
        timeout = handshake_end - time.monotonic()
        if request.deadline:
            timeout = min(timeout, timestamp(request.deadline) - time.time())
        if timeout <= 0:
            raise GripError("deadline_exceeded")
        # Reading during admission prevents pipelined bytes before confirmation.
        opening = asyncio.create_task(opener(request))
        early = asyncio.create_task(connection.recv())
        done, _ = await asyncio.wait([opening, early], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if early in done:
            raise GripError("invalid_message")
        if opening not in done:
            raise GripError("deadline_exceeded")
        opened = opening.result()
        early.cancel()
        await asyncio.gather(early, return_exceptions=True)
        if opened.expires_at is not None and opened.expires_at <= time.time():
            raise GripError("authentication_failed")
        reply = json.dumps({"version": "grip.v1", "request_id": request_id, "kind": "stream-open", "body": opened.body})
        if len(reply.encode()) > MAX_MESSAGE:
            raise GripError("invalid_output")
        await connection.send(reply)
        confirmed = True
        await bridge_streams(WebSocketStream(connection), opened.stream, expires_at=opened.expires_at)
    except (GripError, ValidationError, ValueError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
        if not confirmed and request_id:
            code = exc.code if isinstance(exc, GripError) else "invalid_message" if isinstance(exc, (ValidationError, ValueError)) else "internal_error"
            try:
                await connection.send(json.dumps({"version": "grip.v1", "request_id": request_id, "kind": "error", "error_code": code}))
            except (RuntimeError, OSError):
                pass
    finally:
        for task in (early, opening):
            if task is not None and not task.done():
                task.cancel()
        tasks = [t for t in (early, opening) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Admission may complete just as the peer disconnects.
        if opened is None and opening is not None and not opening.cancelled() and opening.exception() is None:
            opened = opening.result()
        if opened is not None:
            with suppress(OSError, RuntimeError):
                await opened.stream.aclose()
        try:
            await connection.close(code=1000 if confirmed else 1002)
        except (RuntimeError, OSError):
            pass
