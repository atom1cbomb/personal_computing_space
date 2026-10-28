from __future__ import annotations

import asyncio
import time
from typing import Any

from .client import LoomClient


async def poll_submission(client: LoomClient, request_id: str, *, timeout: float | None = None, interval: float = 1.0) -> dict[str, Any]:
    """Poll until the server returns a terminal submission state.

    ``timeout`` is an optional local polling watchdog and does not replace the
    execution or cleanup budgets carried by the submitted Run.
    """
    if (timeout is not None and timeout <= 0) or interval <= 0:
        raise ValueError("poll_budget_invalid")
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        payload = await client.submission(request_id)
        state = str(payload.get("state") or payload.get("current_run_state") or "")
        # Authentication is a deliberate, resumable boundary. Return the
        # waiting projection so the caller can start auth and then poll the
        # same submission again; do not spin until the user finishes login.
        if state in {"completed", "failed", "cancelled", "closed", "awaiting_decision", "awaiting_auth", "repair"}:
            return payload
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("submission_poll_timeout")
        await asyncio.sleep(interval if remaining is None else min(interval, remaining))


__all__ = ["poll_submission"]
