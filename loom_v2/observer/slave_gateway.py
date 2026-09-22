"""Observer-owned relay to the active Slave lease."""

from typing import Any

import httpx


class SlaveForwardError(RuntimeError):
    """A downstream Worker error whose status may be preserved by Observer."""

    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = int(status_code)
        self.body = body if isinstance(body, dict) else {"detail": "slave_request_failed"}
        detail = self.body.get("detail", "slave_request_failed")
        super().__init__(str(detail))


class ObserverSlaveGateway:
    ALLOWED_PATHS = frozenset({
        "/worker/v1/provision", "/worker/v1/dispatch",
        "/worker/v1/deprovision", "/worker/v1/capability/auth/start",
        "/worker/v1/capability/auth/browser", "/worker/v1/inspect",
        "/worker/v1/reconcile",
    })

    def __init__(self, repository: Any, *, workspace_id: str, token: str, timeout: float) -> None:
        self.repository = repository
        self.workspace_id = workspace_id
        self.token = token
        self.timeout = timeout

    async def forward(self, slave_id: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path not in self.ALLOWED_PATHS:
            raise RuntimeError("slave_forward_path_invalid")
        agents = await self.repository.list_agents(self.workspace_id, role="slave")
        agent = next((item for item in agents if str(item.get("agent_id") or "") == slave_id
                      and item.get("lease_state") == "active" and item.get("endpoint_url")), None)
        if agent is None:
            raise RuntimeError("slave_not_available")
        headers = {}
        if self.token:
            headers = {"X-Loom-Internal-Token": self.token, "Authorization": f"Bearer {self.token}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(f"{str(agent['endpoint_url']).rstrip('/')}{path}", json=payload, headers=headers)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RuntimeError("slave_unavailable") from exc
        try:
            value = response.json()
        except ValueError as exc:
            raise RuntimeError("slave_invalid_response") from exc
        if response.status_code >= 400:
            raise SlaveForwardError(response.status_code, value)
        return value if isinstance(value, dict) else {"result": value}
