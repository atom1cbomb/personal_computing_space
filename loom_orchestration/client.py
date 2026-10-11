"""Observer public data-plane client."""

from __future__ import annotations

from typing import Any

import httpx

from .contracts import ClosureContract, ResourceRef


class LoomClient:
    def __init__(self, base_url: str, *, token: str | None = None, client: httpx.AsyncClient | None = None, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client
        self.transport = transport
        self._owns_client = False
        self.timeout = timeout

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
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

    async def upload_content(self, content: Any, *, media_type: str = "application/json") -> ResourceRef:
        payload = await self._request("POST", "/api/v1/content", json={"content": content, "media_type": media_type})
        return ResourceRef.model_validate(payload)

    async def register_package(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/capability-packages", json=manifest)

    async def submit(self, *, request_id: str, plan: ClosureContract, conversation_ref: str | None = None) -> dict[str, Any]:
        payload = {"request_id": request_id, "plan": plan.model_dump(mode="json")}
        if conversation_ref is not None:
            payload["conversation_ref"] = conversation_ref
        return await self._request("POST", "/api/v1/run-submissions", json=payload)

    async def submission(self, request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/run-submissions/{request_id}")

    async def run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/runs/{run_id}")

    async def resources(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/resources")

    async def resource(self, resource_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/resources/{resource_id}")

    async def resource_activations(self, resource_id: str, **filters: str) -> list[dict[str, Any]]:
        result = await self._request("GET", f"/api/v1/resources/{resource_id}/activations", params=filters)
        return result if isinstance(result, list) else list(result.get("activations", []))

    async def start_auth(self, package_ref: str, *, approved_digest: str, target_slave: str, instance_name: str = "default") -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/v1/capability-packages/{package_ref}/auth/start",
            json={"approved_digest": approved_digest, "target_slaves": [target_slave], "instance_name": instance_name},
        )

    async def cancel(self, run_id: str, *, request_id: str, reason: str = "user_cancel") -> dict[str, Any]:
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
