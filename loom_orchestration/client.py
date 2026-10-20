"""Observer public data-plane client."""

from __future__ import annotations

from typing import Any

import httpx
from jsonschema import Draft202012Validator
from uuid import uuid4
from urllib.parse import urlsplit

from .grip import Description, GripError, Names, Reply, Request, decode, uri

from .contracts import ClosureContract, ResourceRef


class LoomClient:
    def __init__(self, base_url: str, *, token: str | None = None, client: httpx.AsyncClient | None = None, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0, grip_authority: str = "loom.local", workspace_id: str = "workspace-default", allow_insecure_grip: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client
        self.transport = transport
        self._owns_client = False
        self.timeout = timeout
        self.workspace_id = workspace_id
        self.names = Names(grip_authority, workspace_id)
        self.allow_insecure_grip = allow_insecure_grip

    async def __aenter__(self) -> "LoomClient":
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.transport)
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
            client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, headers=headers, transport=self.transport)
            close = True
        try:
            response = await client.request(method, path, headers=headers, **kwargs)
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
        base, destination = urlsplit(self.base_url), urlsplit(endpoint)
        if (destination.scheme, destination.netloc) != (base.scheme, base.netloc) or destination.username:
            raise GripError("untrusted_endpoint")
        if destination.query or destination.fragment:
            raise GripError("untrusted_endpoint")
        if destination.scheme != "https" and not (destination.scheme == "http" and self.allow_insecure_grip):
            raise GripError("unsupported_binding")
        # A separate request path: GRIP auth lives exclusively in the body.
        client = self._client or httpx.AsyncClient(transport=self.transport, timeout=self.timeout)
        try:
            response = await client.post(endpoint, json=payload, follow_redirects=False, timeout=self.timeout)
            if response.status_code != 200:
                raise GripError("binding_error")
            if response.headers.get("content-type", "").split(";")[0] != "application/json":
                raise GripError("invalid_message")
            reply = Reply.model_validate(decode(response.content))
            if reply.request_id != payload["request_id"]:
                raise GripError("reply_correlation_mismatch")
            if reply.kind == "error":
                raise GripError(reply.error_code, remote=True)
            return reply.body
        finally:
            if self._client is None:
                await client.aclose()

    def _grip_request(self, method: str, target: str, **fields: Any) -> dict[str, Any]:
        uri(target)
        if target != self.names.root and not target.startswith(self.names.root + "/"):
            raise GripError("untrusted_authority")
        if not self.token:
            raise GripError("authentication_required")
        protocol = "https-json.v1" if urlsplit(self.base_url).scheme == "https" else "loom-http-json.v1"
        return {"version": "grip.v1", "request_id": uuid4().hex, "method": method,
                "auth": {"scheme": "bearer", "credential": self.token}, "target": target,
                "accepted_protocols": [protocol], **fields}

    async def describe(self, target: str) -> dict[str, Any]:
        payload = self._grip_request("DESCRIBE", target)
        body = await self._grip_exchange(self.base_url + "/.well-known/grip", payload)
        description = Description.model_validate(body)
        description.check_target(target)
        return body

    async def invoke(self, target: str, action: str, input: dict[str, Any], *, deadline: str | None = None) -> dict[str, Any]:
        description = Description.model_validate(await self.describe(target))
        offer = next((o for o in description.offers if o.action.uri == action), None)
        if offer is None:
            raise GripError("unsupported_action")
        if offer.action.mode != "request-reply":
            raise GripError("unsupported_binding")
        payload = self._grip_request("INVOKE", target, action=action, input=input,
                                     **({"deadline": deadline} if deadline else {}))
        binding = next((b for b in offer.bindings if b.protocol in payload["accepted_protocols"]), None)
        if binding is None:
            raise GripError("unsupported_binding")
        if binding.audience is not None and binding.audience != self.names.root:
            raise GripError("untrusted_audience")
        # read has a preinstalled bootstrap contract, preventing schema recursion.
        if action == self.names.action("read"):
            if input != {}:
                raise GripError("invalid_input")
            output_schema = {"type": "object"}
        else:
            input_schema = await self.invoke(offer.action.input_schema, self.names.action("read"), {})
            output_schema = await self.invoke(offer.action.output_schema, self.names.action("read"), {})
            if not Draft202012Validator(input_schema).is_valid(input):
                raise GripError("invalid_input")
        # A missing endpoint uses the caller's explicit, trusted Observer mapping.
        result = await self._grip_exchange(binding.endpoint or self.base_url + "/.well-known/grip", payload)
        if not Draft202012Validator(output_schema).is_valid(result):
            raise GripError("invalid_output")
        return result

    async def resources(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        result = await self.invoke(self.names.root, self.names.action("list-resources"),
                                   {"kind": kind} if kind is not None else {})
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
