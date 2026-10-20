"""Describe or invoke one explicitly selected GRIP resource/action.

This script also serves as a live acceptance client. Mutations happen only
when the caller supplies both the action and its explicit JSON input.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from loom_orchestration import LoomClient


async def main(args):
    async with LoomClient(args.observer, token=os.environ["LOOM_GRIP_TOKEN"],
                          grip_authority=args.authority, workspace_id=args.workspace,
                          allow_insecure_grip=args.allow_http) as client:
        target = args.target or client.names.root
        if not args.action:
            return await client.describe(target)
        action = client.names.action(args.action) if not args.action.startswith("grip://") else args.action
        result = await client.invoke(target, action, json.loads(args.input.read_text()))
        if args.wait and "run" in result:
            for _ in range(args.wait):
                state = await client.invoke(result["run"], client.names.action("read"), {})
                if state["state"] in {"completed", "failed", "cancelled", "awaiting_auth", "awaiting_decision"}:
                    return {**result, **state}
                await asyncio.sleep(1)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", default=os.getenv("LOOM_OBSERVER_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--authority", default=os.getenv("LOOM_GRIP_AUTHORITY", "loom.local"))
    parser.add_argument("--workspace", default=os.getenv("LOOM_WORKSPACE_ID", "workspace-default"))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--target", help="exact resource URI returned by discovery")
    parser.add_argument("--action", help="full business action URI, or built-in name such as list-resources")
    parser.add_argument("--input", type=Path, help="JSON input file")
    parser.add_argument("--wait", type=int, default=0, help="maximum seconds to poll a returned Run")
    args = parser.parse_args()
    if args.action and args.input is None:
        parser.error("--action requires --input")
    print(json.dumps(asyncio.run(main(args)), ensure_ascii=False, indent=2))
