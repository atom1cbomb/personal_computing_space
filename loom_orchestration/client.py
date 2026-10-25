"""Observer public data-plane client."""

from __future__ import annotations

from typing import Any
from copy import deepcopy
import asyncio
import time

import httpx
from jsonschema import Draft202012Validator
from uuid import uuid4
from urllib.parse import urlsplit

from .grip import Binding, Description, GripError, Names, Reply, Request, decode, uri
from .bindings import AccessCredential, stream_endpoint, timestamp, validate_endpoint
from .streams import STREAM_PROTOCOL, connect_stream

from .contracts import ClosureContract, ResourceRef


class LoomClient:
    def __init__(self, base_url: str, *, token: str | None = None, client: httpx.AsyncClient | None = None, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0, grip_authority: str = "loom.local", workspace_id: str = "workspace-default", allow_insecure_grip: bool = False, trusted_bindings: list[dict] | None = None, binding_preference: list[str] | None = None, credential_provider=None, descriptions: dict | None = None, schemas: dict | None = None, ssl_context=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client
        self.transport = transport
        self._owns_client = False
        self.timeout = timeout
        self.workspace_id = workspace_id
        self.names = Names(grip_authority, workspace_id)
        self.allow_insecure_grip = allow_insecure_grip
        protocol = "https-json.v1" if urlsplit(self.base_url).scheme == "https" else "loom-http-json.v1"
        self._trusted_bindings = [
            Binding(protocol=protocol, endpoint=self.base_url + "/.well-known/grip", audience=self.names.root),
            Binding(protocol=STREAM_PROTOCOL, endpoint=stream_endpoint(self.base_url), audience=self.names.root),
            *[Binding.model_validate(b) for b in (trusted_bindings or [])],
        ]
        self._trusted_bindings = list({(b.protocol, b.endpoint, b.audience): b for b in self._trusted_bindings}.values())
        self.binding_preference = binding_preference or []
        self.credential_provider = credential_provider
        self.ssl_context = ssl_context
        self._descriptions = deepcopy(descriptions or {})
        self._schemas = deepcopy(schemas or {})
        self._credentials: dict[tuple, tuple[AccessCredential, float]] = {}
        self._credential_lock = asyncio.Lock()

    async def __aenter__(self) -> "LoomClient":
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.transport, verify=self.ssl_context or True, trust_env=False)
            self._owns_client = True
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        client = self._client
        close = False
        if client is None:
            client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, headers=headers, transport=self.transport, verify=self.ssl_context or True, trust_env=False)
            close = True
        try:
            response = await client.request(method, path, headers=headers, follow_redirects=False, **kwargs)
            response.raise_for_status()
            return response.json()
        finally:
            if close:
                await client.aclose()

    async def upload_content(self, content: Any, *, media_type: str = "application/json", name: str | None = None) -> ResourceRef:
        payload = await self._request("POST", "/api/v1/content", json={"content": content, "media_type": media_type, "name": name})
        return ResourceRef.model_validate(payload)

    async def register_package(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Publish a package; ``resource`` is its Observer-assigned GRIP URI."""
        return await self._request("POST", "/api/v1/capability-packages", json=manifest)

    async def packages(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        return await self._request(
            "GET", "/api/v1/capability-packages",
            params={"run_id": run_id} if run_id is not None else {},
        )

    async def descriptor(self, ref: ResourceRef) -> dict[str, Any]:
        if not ref.version_or_digest:
            raise ValueError("action_descriptor_digest_required")
        result = await self._request("GET", "/api/v1/action-descriptors", params={
            "resource_id": ref.resource_id,
            "version_or_digest": ref.version_or_digest,
        })
        return result["descriptor"]

    async def read_json(self, ref: ResourceRef) -> Any:
        from .naming import content_digest

        if content_digest(ref.resource_id) is None:
            raise ValueError("content_ref_required")
        result = await self.invoke(ref.resource_id, self.names.action("read"), {})
        return result["content"]

    async def submit(self, *, request_id: str, plan: ClosureContract, conversation_ref: str | None = None) -> dict[str, Any]:
        payload = {"request_id": request_id, "plan": plan.model_dump(mode="json")}
        if conversation_ref is not None:
            payload["conversation_ref"] = conversation_ref
        return await self._request("POST", "/api/v1/run-submissions", json=payload)

    async def submission(self, request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/run-submissions/{request_id}")

    async def run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/runs/{run_id}")

    async def _grip_exchange(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        Request.model_validate(payload)
        # A separate request path: GRIP auth lives exclusively in the body.
        client = self._client or httpx.AsyncClient(transport=self.transport, timeout=self.timeout, verify=self.ssl_context or True, trust_env=False)
        try:
            timeout = min(self.timeout, timestamp(payload["deadline"]) - time.time()) if payload.get("deadline") else self.timeout
            if timeout <= 0:
                raise GripError("deadline_exceeded")
            request = client.build_request("POST", endpoint, json=payload, timeout=timeout)
            # A supplied AsyncClient may carry Observer auth or session cookies.
            # GRIP authentication is exclusively in the scoped message body.
            for name in list(request.headers):
                if name.lower() not in {"host", "content-length", "content-type", "accept", "accept-encoding", "user-agent", "connection"}:
                    del request.headers[name]
            response = await client.send(request, auth=None, follow_redirects=False)
            if response.status_code != 200:
                raise GripError("binding_error")
            if response.headers.get("content-type", "").split(";")[0] != "application/json":
                raise GripError("invalid_message")
            reply = Reply.model_validate(decode(response.content))
            if reply.request_id != payload["request_id"]:
                raise GripError("reply_correlation_mismatch")
            if reply.kind == "error":
                raise GripError(reply.error_code, remote=True)
            if reply.kind != "reply":
                raise GripError("invalid_message")
            return reply.body
        finally:
            if self._client is None:
                await client.aclose()

    def _grip_request(self, method: str, target: str, *, credential: str | None = None, protocol: str | None = None, **fields: Any) -> dict[str, Any]:
        uri(target)
        if target != self.names.root and not target.startswith(self.names.root + "/"):
            raise GripError("untrusted_authority")
        credential = credential or self.token
        if not credential:
            raise GripError("authentication_required")
        protocol = protocol or ("https-json.v1" if urlsplit(self.base_url).scheme == "https" else "loom-http-json.v1")
        return {"version": "grip.v1", "request_id": uuid4().hex, "method": method,
                "auth": {"scheme": "bearer", "credential": credential}, "target": target,
                "accepted_protocols": [protocol], **fields}

    async def describe(self, target: str, *, refresh: bool = False) -> dict[str, Any]:
        cached = self._descriptions.get(target)
        if not refresh and cached and cached.get("expires_at") and timestamp(cached["expires_at"]) > time.time():
            return deepcopy(cached)
        payload = self._grip_request("DESCRIBE", target)
        validate_endpoint(self._trusted_bindings[0], allow_insecure=self.allow_insecure_grip)
        body = await self._grip_exchange(self.base_url + "/.well-known/grip", payload)
        description = Description.model_validate(body)
        description.check_target(target)
        if description.expires_at:
            self._descriptions[target] = deepcopy(body)
        return body

    def _select_binding(self, offer, requested=None) -> Binding:
        protocols = {STREAM_PROTOCOL} if offer.action.mode == "byte-stream" else {"https-json.v1", "loom-http-json.v1"}
        candidates = list(offer.bindings)
        if requested is not None:
            requested = requested if isinstance(requested, Binding) else Binding.model_validate(requested)
            candidates = [b for b in candidates if b == requested]
        def rank(b):
            return next((i for i, pref in enumerate(self.binding_preference) if pref in (b.endpoint, b.audience)), len(self.binding_preference))
        errors = []
        for candidate in sorted(candidates, key=rank):
            if candidate.protocol not in protocols:
                continue
            audience = candidate.audience or self.names.root
            trusted = [b for b in self._trusted_bindings if b.protocol == candidate.protocol and b.audience == audience
                       and (candidate.endpoint is None or b.endpoint == candidate.endpoint)]
            if len(trusted) != 1:
                errors.append("untrusted_endpoint" if any(b.audience == audience for b in self._trusted_bindings) else "untrusted_audience")
                continue
            result = candidate.model_copy(update={"audience": audience, "endpoint": candidate.endpoint or trusted[0].endpoint})
            validate_endpoint(result, allow_insecure=self.allow_insecure_grip)
            return result
        raise GripError(errors[0] if errors else "unsupported_binding")

    async def _credential(self, binding, target, action, description):
        access = description.resource.metadata.get("service_access") or {}
        if self.credential_provider is not None:
            value = await self.credential_provider(binding, target, action, description)
            return value if isinstance(value, AccessCredential) else AccessCredential(value)
        if action not in access.get("actions", []):
            if binding.audience != self.names.root:
                raise GripError("authentication_required")
            if not self.token:
                raise GripError("authentication_required")
            return AccessCredential(self.token)
        key = (self.token, binding.audience, target, action, access["revision"])
        async with self._credential_lock:
            cached = self._credentials.get(key)
            if cached and cached[1] > time.time():
                return cached[0]
            response = await self._request("POST", "/api/v1/auth/token", json={
                "target": target, "action": action, "audience": binding.audience,
                "expected_activation_revision": access["revision"],
            })
            value = AccessCredential.from_response(response)
            if value.expires_at is None or value.expires_at <= time.time():
                raise GripError("authentication_failed")
            self._credentials[key] = (value, time.time() + (value.expires_at - time.time()) * .8)
            return value

    async def _schema(self, target):
        if target not in self._schemas:
            value = await self.invoke(target, self.names.action("read"), {})
            if "/content/" in target:
                value = value["content"]
            Draft202012Validator.check_schema(value)
            # Only content and versioned Schema identities are immutable caches.
            if "/content/" in target or "/schemas/" in target:
                self._schemas[target] = value
            return value
        return self._schemas[target]

    async def _prepare(self, target, action, input, *, mode, binding=None, deadline=None):
        description = Description.model_validate(await self.describe(target))
        offer = next((o for o in description.offers if o.action.uri == action), None)
        if offer is None:
            raise GripError("unsupported_action")
        if offer.action.mode != mode:
            raise GripError("unsupported_binding")
        selected = self._select_binding(offer, binding)
        # read has a preinstalled bootstrap contract, preventing schema recursion.
        if action == self.names.action("read"):
            if input != {}:
                raise GripError("invalid_input")
            output_schema = {"type": "object"}
        else:
            input_schema = await self._schema(offer.action.input_schema)
            output_schema = await self._schema(offer.action.output_schema)
            if not Draft202012Validator(input_schema).is_valid(input):
                raise GripError("invalid_input")
        credential = await self._credential(selected, target, action, description)
        payload = self._grip_request("INVOKE", target, action=action, input=input,
                                     credential=credential.credential, protocol=selected.protocol,
                                     **({"deadline": deadline} if deadline else {}))
        return selected, payload, output_schema, credential

    async def invoke(self, target: str, action: str, input: dict[str, Any], *, deadline: str | None = None, binding=None) -> dict[str, Any]:
        selected, payload, output_schema, _ = await self._prepare(target, action, input, mode="request-reply", binding=binding, deadline=deadline)
        result = await self._grip_exchange(selected.endpoint, payload)
        if not Draft202012Validator(output_schema).is_valid(result):
            raise GripError("invalid_output")
        if action in {self.names.action(n) for n in ("stop", "configure", "auth-start", "activate-package", "update-attributes")}:
            self._descriptions.clear()
        return result

    async def open_stream(self, target: str, action: str, input: dict[str, Any], *, deadline: str | None = None, binding=None):
        selected, payload, schema, credential = await self._prepare(target, action, input, mode="byte-stream", binding=binding, deadline=deadline)
        timeout = min(self.timeout, timestamp(deadline) - time.time()) if deadline else self.timeout
        if timeout <= 0:
            raise GripError("deadline_exceeded")
        stream = await connect_stream(selected.endpoint, payload, timeout=timeout,
                                      expires_at=credential.expires_at, ssl=self.ssl_context)
        if not Draft202012Validator(schema).is_valid(stream.body):
            await stream.aclose(1002)
            raise GripError("invalid_output")
        return stream

    async def resources(self, *, mcue: str | None = None) -> list[dict[str, Any]]:
        result = await self.invoke(self.names.root, self.names.action("list-resources"),
                                   {"mcue": mcue} if mcue is not None else {})
        return result["resources"]

    async def cancel(self, run_id: str, *, request_id: str | None = None, reason: str = "user_cancel") -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/runs/{run_id}/cancel", json={"request_id": request_id, "reason": reason})

    async def resubmit(self, run_id: str, *, request_id: str, base_closure_version: str, plan: ClosureContract) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/runs/{run_id}/resubmit", json={"request_id": request_id, "base_closure_version": base_closure_version, "plan": plan.model_dump(mode="json")})

    async def resolve(self, run_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"accept", "abandon"}:
            raise ValueError("invalid_decision")
        return await self._request("POST", f"/api/v1/runs/{run_id}/resolve", json={"decision": decision})

    async def close(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/runs/{run_id}/close", json={})


__all__ = ["LoomClient"]
