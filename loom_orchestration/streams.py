"""The loom-wss-stream.v1 binding: one INVOKE and one ordered byte stream.

The binding deliberately has no half-close or automatic reconnect/replay.
Receive queues and messages are bounded; writes await the transport's drain.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any

from websockets.asyncio.client import connect, unix_connect
from websockets.exceptions import ConnectionClosed

from .grip import GripError, Reply, Request, decode

STREAM_PROTOCOL = "loom-wss-stream.v1"
MAX_MESSAGE = 64 * 1024
MAX_QUEUE = 16


class StreamAborted(GripError):
    def __init__(self, code: int = 1011):
        super().__init__("stream_aborted")
        self.close_code = code


class WebSocketStream:
    """A local byte-stream handle, never serialized into a GRIP reply."""

    def __init__(self, websocket: Any, *, body: dict | None = None, expires_at: float | None = None):
        self.websocket = websocket
        self.body = body or {}
        self.expires_at = expires_at
        self._pending = b""
        self._closed = False
        self._close_code = 1000
        self._reader_lock = asyncio.Lock()
        self._writer_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def _remaining(self):
        if self.expires_at is None:
            return None
        remaining = self.expires_at - time.time()
        if remaining <= 0:
            raise StreamAborted(1008)
        return remaining

    async def read(self, max_bytes: int = MAX_MESSAGE) -> bytes:
        if max_bytes < 1:
            raise ValueError("positive_read_size_required")
        async with self._reader_lock:
            while not self._pending:
                if self._closed:
                    if self._close_code != 1000:
                        raise StreamAborted(self._close_code)
                    return b""
                try:
                    message = await asyncio.wait_for(self.websocket.recv(), self._remaining())
                except ConnectionClosed as exc:
                    self._closed = True
                    if exc.rcvd is not None and exc.rcvd.code == 1000:
                        return b""
                    raise StreamAborted(exc.rcvd.code if exc.rcvd else 1006) from exc
                except asyncio.TimeoutError as exc:
                    await self.aclose(1008)
                    raise StreamAborted(1008) from exc
                if message is None:  # ASGI adapter's normal-close indication
                    self._closed = True
                    return b""
                if not isinstance(message, bytes):
                    await self.aclose(1002)
                    raise StreamAborted(1002)
                if len(message) > MAX_MESSAGE:
                    await self.aclose(1009)
                    raise StreamAborted(1009)
                self._pending = message
            result, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
            return result

    async def write(self, data: bytes) -> None:
        async with self._writer_lock:
            if self._closed:
                raise StreamAborted()
            for offset in range(0, len(data), MAX_MESSAGE):
                try:
                    await asyncio.wait_for(self.websocket.send(data[offset:offset + MAX_MESSAGE]), self._remaining())
                except asyncio.TimeoutError as exc:
                    await self.aclose(1008)
                    raise StreamAborted(1008) from exc
                except ConnectionClosed as exc:
                    raise StreamAborted(exc.rcvd.code if exc.rcvd else 1006) from exc

    async def write_eof(self):
        raise GripError("unsupported_binding")

    async def aclose(self, code: int = 1000):
        self._closed = True
        if code != 1000:
            self._close_code = code
        with suppress(ConnectionClosed, RuntimeError, OSError):
            await self.websocket.close(code=code)

    close = aclose


class TCPStream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self._abort_code = None

    async def read(self, max_bytes: int = MAX_MESSAGE):
        if self._abort_code:
            raise StreamAborted(self._abort_code)
        result = await self.reader.read(max_bytes)
        if self._abort_code:
            raise StreamAborted(self._abort_code)
        return result

    async def write(self, data: bytes):
        if self._abort_code:
            raise StreamAborted(self._abort_code)
        self.writer.write(data)
        await self.writer.drain()

    async def aclose(self, code: int = 1000):
        if code != 1000:
            self._abort_code = code
        self.writer.close()
        with suppress(OSError):
            await self.writer.wait_closed()


async def connect_stream(endpoint: str, request: dict, *, timeout: float = 30,
                         expires_at: float | None = None, ssl: Any = None,
                         unix_path: str | None = None) -> WebSocketStream:
    """Send once; a failed/uncertain handshake is never retried."""
    request = Request.model_validate(request).model_dump(exclude_none=True)
    raw = json.dumps(request, separators=(",", ":"), ensure_ascii=False)
    if len(raw.encode()) > MAX_MESSAGE:
        raise GripError("invalid_message")
    options = dict(subprotocols=[STREAM_PROTOCOL], compression=None, max_size=MAX_MESSAGE,
                   max_queue=MAX_QUEUE, write_limit=MAX_MESSAGE, open_timeout=timeout,
                   ping_interval=20, ping_timeout=40, close_timeout=5, proxy=None)
    if ssl is not None:
        options["ssl"] = ssl
    connector = unix_connect(unix_path, uri=endpoint, **options) if unix_path else connect(endpoint, **options)
    handshake_end = time.monotonic() + timeout
    ws = await connector
    try:
        if ws.subprotocol != STREAM_PROTOCOL:
            raise GripError("unsupported_binding")
        async with asyncio.timeout(max(0, handshake_end - time.monotonic())):
            await ws.send(raw)
            message = await ws.recv()
            if not isinstance(message, str):
                raise GripError("invalid_message")
            reply = Reply.model_validate(decode(message.encode()))
            if reply.request_id != request["request_id"]:
                raise GripError("reply_correlation_mismatch")
            if reply.kind == "error":
                raise GripError(reply.error_code, remote=True)
            if reply.kind != "stream-open":
                raise GripError("invalid_message")
        return WebSocketStream(ws, body=reply.body, expires_at=expires_at)
    except BaseException:
        await ws.close()
        raise


async def bridge_streams(left, right, *, expires_at: float | None = None):
    async def copy(source, destination):
        while data := await source.read(MAX_MESSAGE):
            await destination.write(data)

    tasks = [asyncio.create_task(copy(left, right)), asyncio.create_task(copy(right, left))]
    code = 1000
    try:
        remaining = None if expires_at is None else max(0, expires_at - time.time())
        done, _ = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise StreamAborted(1008)
        for task in done:
            task.result()
    except StreamAborted as exc:
        code = exc.close_code
        raise
    except BaseException:
        code = 1011
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # 1006 is a local observation, never a legal outgoing close code.
        if code == 1006:
            code = 1011
        await asyncio.gather(left.aclose(code), right.aclose(code), return_exceptions=True)
