"""Observer binding projection, scoped issuance and optional service relay."""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timezone

import httpx
from fastapi import Depends, HTTPException, Request as HTTPRequest, WebSocket
from loom_v2.auth import InternalAuth
from fastapi.responses import JSONResponse
from pydantic import Field

from loom_orchestration.bindings import stream_endpoint, timestamp, validate_endpoint
from loom_orchestration.grip import Binding, GripError, Model, Reply, Request, URI, decode
from loom_orchestration.stream_server import OpenedStream, serve_stream
from loom_orchestration.streams import STREAM_PROTOCOL, connect_stream
from loom_v2.service_access import ServiceTokens, activation_uri, export_uri, interaction


class TokenInput(Model):
    target: URI
    action: URI
    audience: URI
    expected_activation_revision: int = Field(ge=1)


class DependencyRefresh(Model):
    agent_id: str
    instance_id: str
    lease_id: str
    epoch: int
    workspace_id: str
    activation: URI
    revision: int = Field(ge=1)


class ObserverServiceAccess:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.tokens = ServiceTokens(settings)
        self.names = self.tokens.names
        self.http = httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)

    def _routes(self, package, activation, *, upstream=False):
        values = self.settings.grip_upstream_bindings if upstream else self.settings.grip_data_bindings
        target = activation_uri(self.names, package, activation)
        return [Binding.model_validate(b) for b in values.get(target, values.get(self.names.name("slaves", activation.target_slave), []))]

    def _supports(self, binding, mode):
        return binding.protocol == STREAM_PROTOCOL if mode == "byte-stream" else binding.protocol in {"https-json.v1", "loom-http-json.v1"}

    def upstream(self, package, activation, mode):
        for binding in self._routes(package, activation, upstream=True) or self._routes(package, activation):
            if self._supports(binding, mode):
                validate_endpoint(binding, allow_insecure=self.settings.grip_allow_insecure_services)
                if not binding.audience or binding.audience == self.names.root:
                    raise GripError("untrusted_audience")
                return binding
        raise GripError("unsupported_binding")

    def bindings(self, package, activation, mode):
        if not self.tokens.enabled:
            return []
        result = []
        for binding in self._routes(package, activation):
            if self._supports(binding, mode):
                validate_endpoint(binding, allow_insecure=self.settings.grip_allow_insecure_services)
                if not binding.audience:
                    raise GripError("untrusted_audience")
                result.append(binding.model_dump(exclude_none=True))
        embedded = self.app.state.slaves.get(activation.target_slave)
        relay = embedded is not None
        if not relay:
            try:
                self.upstream(package, activation, mode)
                relay = True
            except GripError as exc:
                if exc.code != "unsupported_binding":
                    raise
        if relay:
            base = self.settings.public_base_url.rstrip("/")
            protocol = STREAM_PROTOCOL if mode == "byte-stream" else "https-json.v1" if base.startswith("https:") else "loom-http-json.v1"
            binding = {"protocol": protocol, "audience": self.names.root}
            if base:
                binding["endpoint"] = stream_endpoint(base) if mode == "byte-stream" else base + "/.well-known/grip"
            if binding not in result:
                result.append(binding)
        return result

    def project(self, package, activation, offers):
        result = []
        for export, offer in zip(package.action_exports, offers, strict=True):
            mode = interaction(export)
            if mode == "task":
                result.append(offer)
            elif bindings := self.bindings(package, activation, mode):
                result.append({"action": offer["action"], "bindings": bindings})
        return result

    def metadata(self, package, activation):
        actions = [export_uri(self.names, package, e) for e in package.action_exports if interaction(e) != "task"]
        if not actions:
            return {}
        return {"service_access": {"activation": activation_uri(self.names, package, activation),
                                   "revision": activation.activation_revision, "actions": actions}}

    async def resolve(self, target, action):
        entry = await self.app.state.grip.resolve(target)
        if not isinstance(entry.value, tuple) or len(entry.value) != 2:
            raise GripError("unsupported_action")
        package, activation = entry.value
        offer = next((o for o in entry.description["offers"] if o["action"]["uri"] == action), None)
        if offer is None:
            raise GripError("unsupported_action")
        export = self.app.state.grip._export(package, action)
        if interaction(export) == "task":
            raise GripError("unsupported_action")
        return entry, package, activation, export, offer

    async def issue_for(self, data, *, subject, caller_revision=None, expires_at=None):
        _, package, activation, _, offer = await self.resolve(data.target, data.action)
        if activation.activation_revision != data.expected_activation_revision:
            raise GripError("forbidden")
        if data.audience not in {b.get("audience") for b in offer["bindings"]}:
            raise GripError("forbidden")
        token, expires = self.tokens.issue(subject=subject, audience=data.audience, target=data.target,
            action=data.action, activation=activation_uri(self.names, package, activation),
            revision=activation.activation_revision, caller_revision=caller_revision, expires_at=expires_at)
        return {"access_token": token, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    async def validate_dependencies(self, dependencies):
        for dependency in dependencies.values():
            for action in dependency.actions:
                await self.resolve(dependency.target, action)

    async def dependency_snapshot(self, caller_uri, revision, agent_id):
        caller = await self.app.state.grip.resolve(caller_uri)
        if not isinstance(caller.value, tuple):
            raise GripError("forbidden")
        _, activation = caller.value
        if activation.target_slave != agent_id or activation.activation_revision != revision or activation.desired_state != "running":
            raise GripError("forbidden")
        result = {"activation": caller_uri, "revision": revision, "observer_url": self.settings.public_base_url or self.settings.observer_url,
                  "workspace_id": self.settings.workspace_id, "grip_authority": self.settings.grip_authority,
                  "allow_insecure_grip": self.settings.grip_allow_insecure_services, "dependencies": {}}
        for name, dependency in activation.dependencies.items():
            description = None
            schemas, credentials, bindings = {}, [], []
            for action in dependency.actions:
                entry, package, target_activation, export, offer = await self.resolve(dependency.target, action)
                description = dict(entry.description)
                description["expires_at"] = datetime.fromtimestamp(time.time() + self.settings.grip_access_token_seconds, timezone.utc).isoformat()
                description["offers"] = [o for o in description["offers"] if o["action"]["uri"] in dependency.actions]
                for field in ("input_schema", "output_schema"):
                    ref = offer["action"].get(field)
                    if ref:
                        schemas[ref] = (await self.app.state.grip.resolve(ref)).value
                for raw in offer["bindings"]:
                    binding = Binding.model_validate(raw)
                    if not binding.endpoint:
                        base = result["observer_url"].rstrip("/")
                        binding = binding.model_copy(update={"endpoint": stream_endpoint(base) if binding.protocol == STREAM_PROTOCOL else base + "/.well-known/grip"})
                    data = TokenInput(target=dependency.target, action=action, audience=binding.audience,
                        expected_activation_revision=target_activation.activation_revision)
                    grant = await self.issue_for(data, subject=caller_uri, caller_revision=revision)
                    credentials.append({"target": dependency.target, "action": action, "audience": binding.audience, **grant})
                    if binding.model_dump() not in bindings:
                        bindings.append(binding.model_dump())
            result["dependencies"][name] = {"target": dependency.target, "actions": dependency.actions,
                "description": description, "schemas": schemas, "credentials": credentials, "bindings": bindings}
        return result

    async def _validate_output(self, package, export, value):
        from loom_v2.contracts.types import IoContract
        from loom_v2.contracts.io_schema import validate
        import json
        contract = IoContract.model_validate_json(await self.app.state.repo.content_store.get(export.io_contract_ref))
        if contract.output_schema_ref:
            schema = json.loads(await self.app.state.repo.content_store.get(contract.output_schema_ref))
            if validate(schema, value):
                raise GripError("internal_error")

    async def _authorized(self, request):
        claims = self.tokens.verify(request, self.names.root)
        entry, package, activation, export, offer = await self.resolve(request.target, request.action)
        if (claims["activation"] != activation_uri(self.names, package, activation)
                or claims["revision"] != activation.activation_revision):
            raise GripError("forbidden")
        if "caller_revision" in claims:
            caller = await self.app.state.grip.resolve(claims["sub"])
            if not isinstance(caller.value, tuple):
                raise GripError("forbidden")
            origin = caller.value[1]
            if (origin.desired_state != "running" or origin.activation_revision != claims["caller_revision"]
                    or not any(d.target == request.target and request.action in d.actions for d in origin.dependencies.values())):
                raise GripError("forbidden")
        schema = (await self.app.state.grip.resolve(offer["action"]["input_schema"])).value
        from jsonschema import Draft202012Validator
        if not Draft202012Validator(schema).is_valid(request.input):
            raise GripError("invalid_input")
        return claims, package, activation, export

    def _delegation(self, request, claims, binding):
        credential, _ = self.tokens.issue(subject=claims["sub"], audience=binding.audience,
            target=request.target, action=request.action, activation=claims["activation"], revision=claims["revision"],
            caller_revision=claims.get("caller_revision"), expires_at=claims["exp"])
        return request.model_copy(update={"auth": request.auth.model_copy(update={"credential": credential}),
                                          "accepted_protocols": [binding.protocol]}).model_dump(exclude_none=True)

    async def invoke(self, request):
        claims, package, activation, export = await self._authorized(request)
        if interaction(export) != "request-reply":
            raise GripError("unsupported_binding")
        slave = self.app.state.slaves.get(activation.target_slave)
        if slave is not None:
            return await slave.grip_data.invoke(request, claims=claims)
        binding = self.upstream(package, activation, "request-reply")
        timeout = min(30, claims["exp"] - time.time())
        if request.deadline:
            timeout = min(timestamp(request.deadline) - time.time(), claims["exp"] - time.time())
        if timeout <= 0:
            raise GripError("deadline_exceeded")
        response = await self.http.post(binding.endpoint, json=self._delegation(request, claims, binding), timeout=timeout)
        if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/json":
            raise GripError("internal_error")
        reply = Reply.model_validate(decode(response.content))
        if reply.request_id != request.request_id or reply.kind == "stream-open":
            raise GripError("invalid_message")
        if reply.kind == "error":
            raise GripError(reply.error_code, remote=True)
        await self._validate_output(package, export, reply.body)
        return reply.body

    async def open_stream(self, request):
        claims, package, activation, export = await self._authorized(request)
        if interaction(export) != "byte-stream":
            raise GripError("unsupported_binding")
        slave = self.app.state.slaves.get(activation.target_slave)
        if slave is not None:
            return await slave.grip_data.open_stream(request, claims=claims)
        binding = self.upstream(package, activation, "byte-stream")
        stream = await connect_stream(binding.endpoint, self._delegation(request, claims, binding), expires_at=claims["exp"])
        try:
            await self._validate_output(package, export, stream.body)
        except BaseException:
            await stream.aclose(1011)
            raise
        return OpenedStream(stream, stream.body, claims["exp"])

    def install(self):
        @self.app.post("/internal/v1/service-dependencies", dependencies=[Depends(InternalAuth(self.settings.internal_api_secret))])
        async def dependencies(data: DependencyRefresh):
            try:
                if data.workspace_id != self.settings.workspace_id:
                    raise GripError("forbidden")
                await self.app.state.repo.heartbeat_agent(data.agent_id, data.instance_id, data.lease_id, data.epoch,
                    workspace_id=data.workspace_id, role="slave")
                result = await self.dependency_snapshot(data.activation, data.revision, data.agent_id)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except (GripError, ValueError, KeyError) as exc:
                raise HTTPException(403, "dependency_access_denied") from exc

        @self.app.post("/api/v1/auth/token")
        async def token(http: HTTPRequest, data: TokenInput):
            authorization = http.headers.get("authorization", "")
            expected = "Bearer " + self.settings.grip_token
            if not self.settings.grip_token or not hmac.compare_digest(authorization.encode(), expected.encode()):
                raise HTTPException(401, "authentication_failed")
            try:
                result = await self.issue_for(data, subject=self.names.name("identities", "owner"))
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except GripError as exc:
                raise HTTPException(403, exc.code) from exc

        @self.app.websocket("/grip/stream")
        async def stream(ws: WebSocket):
            await serve_stream(ws, self.open_stream)
