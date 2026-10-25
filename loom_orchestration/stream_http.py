"""HTTPX transport over a GRIP byte stream, parsed exclusively by httpcore."""
from __future__ import annotations

import asyncio

import httpcore
import httpx

from .grip import GripError


class StreamNetwork(httpcore.AsyncNetworkStream):
    def __init__(self, stream):
        self.stream = stream

    async def read(self, max_bytes, timeout=None):
        async with asyncio.timeout(timeout):
            return await self.stream.read(max_bytes)

    async def write(self, buffer, timeout=None):
        async with asyncio.timeout(timeout):
            await self.stream.write(buffer)

    async def aclose(self):
        await self.stream.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # The current service target is private HTTP. WSS secures the GRIP hop.
        raise GripError("unsupported_binding")


class StreamBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, opener, host, port):
        self.opener, self.host, self.port = opener, host, port

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if (host, port) != (self.host, self.port):
            raise GripError("forbidden")
        async with asyncio.timeout(timeout):
            return StreamNetwork(await self.opener())

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise GripError("unsupported_binding")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class StreamHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, opener, *, host="model.internal", port=80, max_connections=16):
        self.pool = httpcore.AsyncConnectionPool(network_backend=StreamBackend(opener, host, port),
            max_connections=max_connections, max_keepalive_connections=0, retries=0)

    async def handle_async_request(self, request):
        core = httpcore.Request(method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        response = await self.pool.handle_async_request(core)
        return httpx.Response(response.status, headers=response.headers,
            stream=ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()
