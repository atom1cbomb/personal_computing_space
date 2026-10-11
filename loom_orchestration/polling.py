from __future__ import annotations

import asyncio
import time
from typing import Any

from .client import LoomClient


async def poll_submission(client: LoomClient, request_id: str, *, timeout: float = 3600.0, interval: float = 1.0) -> dict[str, Any]:
    if timeout <= 0 or interval <= 0:
        raise ValueError("poll_budget_invalid")
    deadline = time.monotonic() + timeout
    while True:
        payload = await client.submission(request_id)
        state = str(payload.get("state") or payload.get("current_run_state") or "")
        if state in {"completed", "failed", "cancelled", "closed", "awaiting_decision", "repair"}:
            return payload
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("submission_poll_timeout")
        await asyncio.sleep(min(interval, remaining))


__all__ = ["poll_submission"]
