"""Optional GRIP entry for local activations; never exposes Worker methods."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request as HTTPRequest, WebSocket
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from loom_orchestration.bindings import timestamp
from loom_orchestration.grip import GripError, REQUEST_ID, Request, decode
from loom_orchestration.stream_server import OpenedStream, serve_stream
from loom_orchestration.streams import StreamAborted
from loom_v2.contracts.io_schema import validate
from loom_v2.contracts.types import ExecutionContract
from loom_v2.service_access import ServiceTokens, activation_uri, export_uri, interaction


class ManagedStream:
    def __init__(self, stream, release):
        self.stream, self.release = stream, release
        self.close_code = 1000

    async def read(self, n=65536):
        value = await self.stream.read(n)
        if self.close_code != 1000:
            raise StreamAborted(self.close_code)
        return value

    async def write(self, data):
        await self.stream.write(data)

    async def aclose(self, code=1000):
        if code != 1000:
            self.close_code = code
        try:
            await self.stream.aclose(code)
        finally:
            self.release()


class SlaveGripData:
    def __init__(self, service):
        self.service = service
        self.tokens = ServiceTokens(service.settings)
        self.audience = service.names.name("slaves", service.slave_id)
        self.calls = {}
        self.schemas = {}

    def _resolve(self, request, claims):
        service = self.service
        for key, activation in service.activations.items():
            package = service._find_cached_package(activation.package_version_ref, activation.package_digest)
            if package is None or activation_uri(service.names, package, activation) != claims["activation"]:
                continue
            target = activation.target_resource_ref.resource_id if activation.target_resource_ref else None
            if request.target not in {claims["activation"], target}:
                raise GripError("forbidden")
            if activation.desired_state != "running" or activation.activation_state != "ready" or activation.activation_revision != claims["revision"]:
                raise GripError("forbidden")
            for export in package.action_exports:
                if export_uri(service.names, package, export) == request.action:
                    return key, package, activation, export
            raise GripError("unsupported_action")
        raise GripError("not_found")

    def _check_caller(self, claims):
        if "caller_revision" not in claims:
            return
        for activation in self.service.activations.values():
            package = self.service._find_cached_package(activation.package_version_ref, activation.package_digest)
            if package and activation_uri(self.service.names, package, activation) == claims["sub"]:
                if activation.desired_state != "running" or activation.activation_revision != claims["caller_revision"]:
                    raise GripError("forbidden")
                if not any(d.target == claims["target"] and claims["action"] in d.actions for d in activation.dependencies.values()):
                    raise GripError("forbidden")

    async def _validate(self, export, value, *, output=False):
        contract = await self.service._load_io_contract(export.io_contract_ref)
        ref = contract.output_schema_ref if output else contract.input_schema_ref
        if ref is not None:
            if ref.resource_id not in self.schemas:
                self.schemas[ref.resource_id] = json.loads(await self.service.content_store.get(ref))
            if validate(self.schemas[ref.resource_id], value):
                raise GripError("internal_error" if output else "invalid_input")
        if output and contract.success_semantics and contract.success_validator_ref is None:
            raise GripError("internal_error")
        if output and contract.success_validator_ref is not None:
            program = await self.service.content_store.get(contract.success_validator_ref)
            result = await self.service.executor_registry.invoke_for("function", ExecutionContract(kind="process:json_stdio", version="1"), value, program=program)
            if not isinstance(result.value, dict) or result.value.get("result") != "pass":
                raise GripError("internal_error")

    @asynccontextmanager
    async def admission(self, request, claims, mode):
        if request.method != "INVOKE":
            raise GripError("unsupported_action")
        self._check_caller(claims)
        key, _, _, _ = self._resolve(request, claims)
        lock = self.service._activation_locks.setdefault(key, asyncio.Lock())
        call_id = uuid4().hex
        async with lock:
            _, package, activation, export = self._resolve(request, claims)
            if interaction(export) != mode:
                raise GripError("unsupported_action")
            if sum(c["activation"] == claims["activation"] for c in self.calls.values()) >= self.service.settings.grip_stream_max_concurrency:
                raise GripError("internal_error")
            await self._validate(export, request.input)
            self.calls[call_id] = {"activation": claims["activation"], "subject": claims["sub"], "task": asyncio.current_task(), "stream": None}
            snapshot = activation.model_dump(mode="json")
            snapshot["workspace_id"] = self.service.workspace_id
        try:
            yield call_id, package, export, snapshot
        finally:
            current = self.calls.get(call_id)
            if current is not None and current["stream"] is None:
                self.calls.pop(call_id, None)

    async def invoke(self, request, *, claims=None):
        claims = claims or self.tokens.verify(request, self.audience)
        async with self.admission(request, claims, "request-reply") as (call_id, package, export, snapshot):
            timeout = min(30.0, claims["exp"] - time.time())
            if request.deadline:
                timeout = min(timestamp(request.deadline) - time.time(), claims["exp"] - time.time())
            if timeout <= 0:
                raise GripError("deadline_exceeded")
            async with asyncio.timeout(timeout):
                result = await self.service.runtime_plugin_host.invoke(package, export, request.input,
                    activation=snapshot, attempt_id=call_id, deadline_seconds=timeout)
                value = result.get("value")
                if not isinstance(value, dict):
                    raise GripError("internal_error")
                await self._validate(export, value, output=True)
            return value

    async def open_stream(self, request, *, claims=None):
        claims = claims or self.tokens.verify(request, self.audience)
        async with self.admission(request, claims, "byte-stream") as (call_id, package, export, snapshot):
            opened = await self.service.runtime_plugin_host.open_stream(package, export, request.input,
                activation=snapshot, request_id=call_id, deadline=request.deadline)
            try:
                await self._validate(export, opened.body, output=True)
            except BaseException:
                await opened.aclose(1011)
                raise
            managed = ManagedStream(opened, lambda: self.calls.pop(call_id, None))
            self.calls[call_id]["stream"] = managed
            return OpenedStream(managed, opened.body, claims["exp"])

    async def revoke(self, target):
        calls = [c for c in self.calls.values() if target in (c["activation"], c["subject"])]
        tasks = []
        for call in calls:
            if call["stream"] is not None:
                await call["stream"].aclose(1008)
            elif call["task"] is not asyncio.current_task():
                call["task"].cancel()
                tasks.append(call["task"])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def create_data_app(service):
    app = FastAPI(title="GRIP service entry", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/.well-known/grip")
    async def invoke(http: HTTPRequest):
        request_id = None
        try:
            raw = decode(await http.body())
            if isinstance(raw, dict) and isinstance(raw.get("request_id"), str) and REQUEST_ID.fullmatch(raw["request_id"]):
                request_id = raw["request_id"]
            request = Request.model_validate(raw)
            protocols = {"https-json.v1"}
            if service.settings.grip_allow_insecure_services:
                protocols.add("loom-http-json.v1")
            if not protocols.intersection(request.accepted_protocols):
                raise GripError("unsupported_binding")
            result = await service.grip_data.invoke(request)
            return JSONResponse({"version": "grip.v1", "request_id": request_id, "kind": "reply", "body": result})
        except (GripError, ValidationError, ValueError, RuntimeError, TimeoutError) as exc:
            if request_id is None:
                return JSONResponse({"error": "invalid_message"}, status_code=400)
            code = exc.code if isinstance(exc, GripError) else "invalid_message" if isinstance(exc, (ValueError, ValidationError)) else "deadline_exceeded" if isinstance(exc, TimeoutError) else "internal_error"
            return JSONResponse({"version": "grip.v1", "request_id": request_id, "kind": "error", "error_code": code})

    @app.websocket("/grip/stream")
    async def stream(ws: WebSocket):
        await serve_stream(ws, service.grip_data.open_stream)

    return app
