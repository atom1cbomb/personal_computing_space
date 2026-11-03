"""Observer-to-Pool task calls. Only the declared task API is exposed."""
import httpx
from loom_v2.contracts.errors import DomainError, DomainErrorEnvelope
from .rpc import encode, decode

TASK_METHODS = frozenset({
    "submit_action", "native_driver_metadata", "native_read_run", "native_submission", "native_invoke",
    "submit_run_plan", "resubmit_run_plan", "get_execution_request", "list_execution_requests",
    "list_run_summaries", "list_plan_summaries", "read_run_result", "read_frozen_plan",
    "get_run", "cancel_run", "resolve_run", "close_run", "read_run_history",
    "recover_activation_wait",
    "abandon_capability_package", "set_capability_desired", "record_capability_health",
})
RESOURCE_METHODS = frozenset({
    "register_resource", "update_resource_attributes", "register_workspace_package", "promote_capability_package",
    "set_capability_desired", "record_capability_health", "register_agent", "heartbeat_agent", "release_agent",
})


class PoolClient:
    methods = TASK_METHODS
    path = "/internal/v1/tasks"

    def __init__(self, url, settings, *, transport=None):
        self.http = httpx.AsyncClient(base_url=url, transport=transport, timeout=httpx.Timeout(settings.worker_action_timeout_seconds, connect=5),
            headers={"X-Loom-Internal-Token": settings.internal_api_secret}, trust_env=False)

    async def close(self):
        await self.http.aclose()

    def __getattr__(self, name):
        if name not in self.methods:
            raise AttributeError(name)
        async def call(*args, **kwargs):
            response = await self.http.post(self.path, json={"method": name, "args": encode(args), "kwargs": encode(kwargs)})
            response.raise_for_status()
            body = response.json()
            if "error" in body:
                error = body["error"]
                if error["type"] == "DomainError":
                    raise DomainError(DomainErrorEnvelope.model_validate(error["value"]))
                cls = {"KeyError": KeyError, "ValueError": ValueError, "RuntimeError": RuntimeError}[error["type"]]
                raise cls(error["value"])
            return decode(body["result"])
        return call


class ResourceClient(PoolClient):
    methods = RESOURCE_METHODS
    path = "/internal/v1/resources/commands"


async def invoke(repository, body, allowed, *, native=None):
    name = body.get("method")
    if name not in allowed:
        from fastapi import HTTPException
        raise HTTPException(400, "command_not_allowed")
    try:
        target = native.grip._submit if name == "submit_action" else getattr(native, name.removeprefix("native_")) if name.startswith("native_") else getattr(repository, name)
        value = await target(*decode(body["args"]), **decode(body["kwargs"]))
        return {"result": encode(value)}
    except DomainError as exc:
        return {"error": {"type": "DomainError", "value": exc.envelope.model_dump(mode="json")}}
    except (KeyError, ValueError, RuntimeError) as exc:
        return {"error": {"type": type(exc).__name__, "value": str(exc.args[0]) if exc.args else str(exc)}}
