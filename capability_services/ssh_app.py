"""Existing SSH private hooks and GRIP file streams on one ASGI service."""
import asyncio
from contextlib import asynccontextmanager
import json
import time

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from loom_orchestration.bindings import timestamp
from loom_orchestration.grip import GripError
from loom_orchestration.stream_server import OpenedStream, serve_stream
from .ssh_execution import GatewayError, MAX_BODY
from .ssh_files import PROTOCOL, SSHFileStream, validate_transfer


def create_app(gateway):
    @asynccontextmanager
    async def lifespan(app):
        yield
        if hasattr(gateway, "close"):
            await asyncio.to_thread(gateway.close)

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/{path:path}")
    async def call(path: str, request: Request):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    raise GatewayError("request_too_large", 413)
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise GatewayError("request_json_invalid")
            return await asyncio.to_thread(gateway.handle, "/v1/" + path, payload)
        except GatewayError as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        except ValueError:
            return JSONResponse({"error": "request_json_invalid"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "gateway_error"}, status_code=500)

    @app.websocket("/v1/files/{action}")
    async def files(websocket: WebSocket, action: str):
        async def opener(request):
            try:
                timeout = validate_transfer(action, request.input)
                ssh_args = await asyncio.to_thread(gateway.execution_ssh_args)
            except GatewayError as exc:
                code = "authentication_failed" if exc.status == 401 else "invalid_input" if exc.status == 400 else "resource_unavailable"
                raise GripError(code) from exc
            expires = time.time() + timeout
            if request.deadline:
                expires = min(expires, timestamp(request.deadline))
            stream = await SSHFileStream.open(ssh_args, action, request.input)
            return OpenedStream(stream, {"protocol": PROTOCOL, "direction": action}, expires_at=expires)
        await serve_stream(websocket, opener)

    return app
