"""Private HTTP hooks and GRIP archive streams for one Overleaf account."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import struct
import time

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator

from loom_orchestration.grip import GripError
from loom_orchestration.bindings import timestamp
from loom_orchestration.stream_server import OpenedStream, serve_stream

from .common import Journal, OverleafError, digest
from .git_store import GitStore
from .web import WebSession

SCHEMAS = Path(__file__).with_name("schemas")
PROTOCOL = "overleaf-archive.v1"
MAX_JSON = 64 * 1024


def frame(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_JSON:
        raise OverleafError("result_too_large", 500)
    return struct.pack("!I", len(data)) + data


class Gateway:
    def __init__(self, *, state_root=Path("/var/lib/loom/service"),
                 credential_root=Path("/run/loom/credentials"), instance_path=Path("/run/loom/instance.json"), transport=None):
        config = json.loads(instance_path.read_text()) if instance_path.exists() else {}
        if not Draft202012Validator(json.loads((SCHEMAS / "instance-config.json").read_text())).is_valid(config):
            raise OverleafError("instance_config_invalid", 500)
        self.journal = Journal(state_root)
        self.web = WebSession(state_root, self.journal, transport=transport)
        self.git = GitStore(state_root / "projects", credential_root / "overleaf_git_token", self.journal)
        self.validators = {name: Draft202012Validator(json.loads((SCHEMAS / f"projects-{name}-input.json").read_text()))
                           for name in ("list", "status", "download", "upload", "import")}

    def validate(self, action, arguments):
        if action not in self.validators or not self.validators[action].is_valid(arguments):
            raise OverleafError("invalid_input")
        if action == "import" and (not arguments["name"].strip() or arguments["name"] in (".", "..")):
            raise OverleafError("invalid_input")

    def query(self, action, arguments):
        self.validate(action, arguments)
        try:
            if action == "list":
                result = self.web.list_projects(arguments)
            elif action == "status":
                result = self.git.status(**arguments)
            else:
                raise OverleafError("action_not_allowed", 404)
            return {"status": "ok", "result": result, "error_code": None}
        except OverleafError as exc:
            return {"status": "error", "result": None, "error_code": exc.code}

    def transfer(self, action, args, data):
        try:
            if action == "download":
                metadata, archive = self.git.download(args["project_id"])
                return frame({"status": "downloaded", **metadata}) + archive
            if len(data) != args["archive_size"] or digest(data) != args["archive_sha256"]:
                raise OverleafError("archive_integrity_failed")
            result = self.git.upload(args, data) if action == "upload" else self.web.import_project(args, data)
            return frame(result)
        except OverleafError as exc:
            return frame(exc.result())
        except (OSError, ValueError):
            return frame({"status": "error", "error_code": "operation_failed"})


class ArchiveStream:
    """Length-delimited input: EOF before declared length never mutates upstream."""
    def __init__(self, gateway, action, args, release):
        self.gateway, self.action, self.args, self.release = gateway, action, args, release
        self.buffer = bytearray()
        self.ready = asyncio.Event()
        self.task = None
        self.output = None
        self.offset = 0
        self.closed = False
        if action == "download":
            self.start()

    def start(self):
        self.task = asyncio.create_task(asyncio.to_thread(self.gateway.transfer, self.action, self.args, bytes(self.buffer)))
        self.buffer.clear()
        self.ready.set()

    async def write(self, data):
        if self.closed or self.action == "download" or self.task or len(self.buffer)+len(data) > self.args["archive_size"]:
            raise GripError("invalid_input")
        self.buffer.extend(data)
        if len(self.buffer) == self.args["archive_size"]:
            self.start()

    async def read(self, max_bytes=65536):
        await self.ready.wait()
        if self.output is None:
            self.output = await asyncio.shield(self.task)
        chunk = self.output[self.offset:self.offset+max_bytes]
        self.offset += len(chunk)
        return chunk

    async def aclose(self, code=1000):
        if not self.closed:
            self.closed = True
            self.buffer.clear()
            # Fully received mutations finish and journal their outcome even if
            # the caller disconnects. No half-close or transport replay is used.
            if self.task:
                def finished(task):
                    if not task.cancelled():
                        task.exception()
                    self.release()
                self.task.add_done_callback(finished)
            else:
                self.release()


def create_app(gateway: Gateway):
    @asynccontextmanager
    async def lifespan(app):
        yield
        gateway.web.close()

    app = FastAPI(lifespan=lifespan)
    active = 0

    @app.exception_handler(OverleafError)
    async def error_handler(request, exc):
        return JSONResponse(exc.result(), status_code=exc.status)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/{category}/{action}")
    async def call(category: str, action: str, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 128 * 1024:
                raise OverleafError("request_too_large", 413)
        try:
            args = json.loads(raw)
        except ValueError as exc:
            raise OverleafError("invalid_input") from exc
        if not isinstance(args, dict):
            raise OverleafError("invalid_input")
        if category == "auth":
            hooks = {"start": gateway.web.auth_start, "status": gateway.web.auth_status, "browser": gateway.web.browser}
            if action not in hooks or action == "status" and args:
                raise OverleafError("invalid_input")
            return await asyncio.to_thread(hooks[action], *(() if action == "status" else (args,)))
        if category != "projects" or action not in ("list", "status"):
            raise OverleafError("action_not_allowed", 404)
        return await asyncio.to_thread(gateway.query, action, args)

    @app.websocket("/v1/projects/{action}")
    async def stream(websocket: WebSocket, action: str):
        async def opener(request):
            nonlocal active
            if action not in ("download", "upload", "import"):
                raise GripError("action_not_found")
            try:
                gateway.validate(action, request.input)
            except OverleafError as exc:
                raise GripError("invalid_input") from exc
            if active >= 2:
                raise GripError("resource_unavailable")
            active += 1

            def release():
                nonlocal active
                active -= 1

            return OpenedStream(ArchiveStream(gateway, action, request.input, release),
                                {"protocol": PROTOCOL, "direction": "download" if action == "download" else "upload"},
                                expires_at=timestamp(request.deadline) if request.deadline else time.time()+300)

        # All Git/network work happens after admission, outside the handshake budget.
        await serve_stream(websocket, opener)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(Gateway()), host="0.0.0.0", port=8080)
