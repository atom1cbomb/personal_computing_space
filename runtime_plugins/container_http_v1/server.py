"""Unix-socket HTTP/JSON process for the container HTTP runtime bundle."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
from typing import Any
from fastapi import Request, WebSocket

from .runtime import DockerContainerHTTPRuntimeV1, RUNTIME_DESCRIPTOR


MAX_REQUEST_BYTES = 8 * 1024 * 1024


class PluginServer:
    def __init__(self, runtime: DockerContainerHTTPRuntimeV1) -> None:
        self.runtime = runtime

    async def dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any] | bytes, str]:
        if method == "GET" and path == "/healthz":
            return 200, b"ok", "text/plain"
        if method == "GET" and path == "/v1/descriptor":
            return 200, dict(RUNTIME_DESCRIPTOR), "application/json"
        if method != "POST" or payload is None:
            return 404, {"error": "not_found"}, "application/json"
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            return 400, {"error": "request_id_required"}, "application/json"
        try:
            if path == "/v1/provision":
                result = await self.runtime.provision(
                    _mapping(payload, "package"),
                    _mapping(payload, "command"),
                )
            elif path == "/v1/invoke":
                business_payload = payload.get("payload")
                if not isinstance(business_payload, dict):
                    raise RuntimeError("request_json_invalid")
                deadline = payload.get("deadline_seconds")
                result = await self.runtime.invoke(
                    _mapping(payload, "package"),
                    _mapping(payload, "action_export"),
                    business_payload,
                    activation=_mapping(payload, "activation"),
                    attempt_id=str(payload.get("attempt_id") or request_id),
                    deadline_seconds=(
                        float(deadline) if deadline is not None else None
                    ),
                )
            elif path == "/v1/inspect":
                result = await self.runtime.inspect(
                    _mapping(payload, "activation")
                )
            elif path == "/v1/auth/start":
                result = await self.runtime.auth_start(
                    _mapping(payload, "package"),
                    _mapping(payload, "activation"),
                )
            elif path == "/v1/auth/browser":
                browser_payload = payload.get("payload")
                if not isinstance(browser_payload, dict):
                    raise RuntimeError("request_json_invalid")
                result = await self.runtime.auth_browser(
                    _mapping(payload, "package"),
                    _mapping(payload, "activation"),
                    browser_payload,
                )
            elif path == "/v1/deprovision":
                result = await self.runtime.deprovision(
                    _mapping(payload, "command")
                )
            elif path == "/v1/reconcile":
                activations = payload.get("activations")
                if not isinstance(activations, list) or not all(
                    isinstance(item, dict) for item in activations
                ):
                    raise RuntimeError("request_json_invalid")
                result = await self.runtime.reconcile(activations)
            else:
                return 404, {"error": "not_found"}, "application/json"
            return (
                200,
                {"request_id": request_id, "ok": True, "result": result},
                "application/json",
            )
        except Exception as exc:
            code = str(exc) or "runtime_plugin_error"
            return (
                200,
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": code,
                        "retryable": code.endswith("timeout")
                        or code
                        in {
                            "service_runtime_unavailable",
                            "runtime_plugin_unavailable",
                        },
                        "safe_message": code,
                        "details": {},
                    },
                },
                "application/json",
            )

    def app(self):
        from fastapi import FastAPI, Request, WebSocket
        from fastapi.responses import Response, JSONResponse
        from loom_orchestration.stream_server import OpenedStream, serve_stream
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.websocket("/v1/stream")
        async def stream(websocket: WebSocket):
            async def open_runtime(request):
                payload = request.input
                opened = await self.runtime.open_stream(_mapping(payload, "package"),
                    _mapping(payload, "action_export"), _mapping(payload, "payload"),
                    activation=_mapping(payload, "activation"), request_id=request.request_id,
                    deadline=request.deadline)
                return OpenedStream(opened, opened.body)
            await serve_stream(websocket, open_runtime)

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def dispatch(request: Request, path: str):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_REQUEST_BYTES:
                    return Response(status_code=413)
            try:
                if request.method == "POST" and request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return Response(status_code=400)
                payload = json.loads(body) if body else None
                if payload is not None and not isinstance(payload, dict):
                    return Response(status_code=400)
            except (ValueError, UnicodeError):
                return Response(status_code=400)
            status, value, content_type = await self.dispatch(request.method, "/" + path, payload)
            if isinstance(value, bytes):
                return Response(value, status_code=status, media_type=content_type)
            return JSONResponse(value, status_code=status)
        return app


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError("request_json_invalid")
    return value


async def main() -> None:
    socket_path = Path(os.environ["LOOM_RUNTIME_PLUGIN_SOCKET"])
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id=os.getenv("LOOM_SLAVE_ID")
        or os.getenv("LOOM_SERVICE_NAME")
        or "slave",
        network=os.getenv("LOOM_RUNTIME_PLUGIN_NETWORK", "loom-internal"),
        memory=os.getenv("LOOM_RUNTIME_PLUGIN_MEMORY", "512m"),
        cpus=float(os.getenv("LOOM_RUNTIME_PLUGIN_CPUS", "1")),
        pids_limit=int(os.getenv("LOOM_RUNTIME_PLUGIN_PIDS_LIMIT", "128")),
        tmpfs_size=os.getenv("LOOM_RUNTIME_PLUGIN_TMPFS_SIZE", "64m"),
        startup_timeout=float(
            os.getenv("LOOM_RUNTIME_PLUGIN_STARTUP_TIMEOUT", "30")
        ),
        request_timeout=float(
            os.getenv("LOOM_RUNTIME_PLUGIN_REQUEST_TIMEOUT", "30")
        ),
        response_max_bytes=int(
            os.getenv(
                "LOOM_RUNTIME_PLUGIN_RESPONSE_MAX_BYTES",
                str(256 * 1024 * 1024),
            )
        ),
        max_concurrency=int(
            os.getenv("LOOM_RUNTIME_PLUGIN_MAX_CONCURRENCY", "16")
        ),
        docker_timeout=float(
            os.getenv("LOOM_RUNTIME_PLUGIN_DOCKER_TIMEOUT", "120")
        ),
        credential_dir=os.getenv("LOOM_RUNTIME_PLUGIN_CREDENTIAL_DIR"),
        egress_network=os.getenv("LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK"),
        instance_state_dir=os.getenv("LOOM_RUNTIME_PLUGIN_INSTANCE_STATE_DIR"),
    )
    server = PluginServer(runtime)
    import socket
    import uvicorn
    from loom_orchestration.streams import MAX_MESSAGE, MAX_QUEUE
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o117)
    try:
        listener.bind(str(socket_path))
    finally:
        os.umask(old_umask)
    listener.listen(128)
    listener.setblocking(False)
    runner = uvicorn.Server(uvicorn.Config(server.app(), lifespan="off", access_log=False,
        ws_max_size=MAX_MESSAGE, ws_max_queue=MAX_QUEUE, ws_ping_interval=20, ws_ping_timeout=40))
    try:
        await runner.serve(sockets=[listener])
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
