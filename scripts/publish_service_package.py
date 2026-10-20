"""Upload a capability_services directory and print its immutable GRIP URI."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from loom_orchestration import LoomClient, ServicePackage


async def publish(args):
    dependencies = json.loads(args.dependencies.read_text()) if args.dependencies else {}
    async with LoomClient(
        args.observer, token=os.environ["LOOM_GRIP_TOKEN"],
        grip_authority=args.authority, workspace_id=args.workspace,
        allow_insecure_grip=args.allow_http,
    ) as client:
        return await ServicePackage(args.directory).publish(client, image_ref=args.image, dependencies=dependencies)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--image", required=True, help="immutable registry/repository@sha256:digest")
    parser.add_argument("--observer", default=os.getenv("LOOM_OBSERVER_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--authority", default=os.getenv("LOOM_GRIP_AUTHORITY", "loom.local"))
    parser.add_argument("--workspace", default=os.getenv("LOOM_WORKSPACE_ID", "workspace-default"))
    parser.add_argument("--dependencies", type=Path, help="JSON map: declared package coordinate -> published GRIP URI")
    parser.add_argument("--allow-http", action="store_true", help="explicit local-development HTTP binding")
    args = parser.parse_args()
    print(asyncio.run(publish(args)))


if __name__ == "__main__":
    main()
