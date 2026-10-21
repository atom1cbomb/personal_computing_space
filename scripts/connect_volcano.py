"""Register a Volcano resource and activate its package through Observer GRIP."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from capability_services.volcano_http.server import NAME_RE, https_origin
from loom_orchestration import LoomClient

ROOT = Path(__file__).resolve().parents[1]


async def connect(client: LoomClient, *, package: str, slave: str, name: str,
                  dashboard_url: str, kubernetes_ref: str, display_name: str | None = None,
                  api_origin: str | None = None):
    origin = https_origin(dashboard_url)
    endpoint = https_origin(api_origin if api_origin is not None else dashboard_url)
    if not NAME_RE.fullmatch(name):
        raise ValueError("instance_name_invalid")
    related = await client.describe(kubernetes_ref)
    if related["resource"]["mcue"] != "capability/activation":
        raise ValueError("kubernetes_activation_required")
    kubernetes_package = await client.invoke(related["resource"]["metadata"]["package"], client.names.action("read"), {})
    if kubernetes_package["package_id"] != "kubernetes-http":
        raise ValueError("kubernetes_activation_required")
    published = await client.invoke(package, client.names.action("read"), {})
    if published["package_id"] != "volcano-http":
        raise ValueError("volcano_package_required")
    definition = json.loads((ROOT / "loom_v2/mcue_types/computing-volcano-v1.json").read_text())
    mcue = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), definition)
    requested_uri = client.names.name("resources", "volcano", name)
    attributes = {"display_name": display_name or name + " Volcano", "dashboard_url": origin,
                  "description": "Volcano Dashboard queues and workload visibility.",
                  "kubernetes_ref": related["resource"]["uri"]}
    existing = next((r for r in await client.resources(mcue="computing/volcano") if r["uri"] == requested_uri), None)
    if existing is None:
        resource = await client.invoke(client.names.root, client.names.action("register-resource"), {
            "resource_id": requested_uri, "mcue_ref": mcue["resource"], "attributes": attributes,
            "package": package, "state": "registered",
        })
        target = resource["resource"]
    else:
        target = existing["uri"]
    activation = await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": name, "target": target,
        "instance_config": {"api_origin": endpoint},
        "idempotency_key": f"connect-volcano-{name}-{published['package_digest'][:16]}",
    })
    if existing is not None:
        before = await client.describe(target)
        metadata = before["resource"]["metadata"]
        if any(metadata["attributes"].get(key) != value for key, value in attributes.items()):
            await client.invoke(target, client.names.action("update-attributes"), {
                "expected_attributes_revision": metadata["attributes_revision"], "patch": attributes,
            })
    description = await client.describe(target)
    assert description["resource"]["metadata"]["attributes"]["kubernetes_ref"] == kubernetes_ref
    return {"resource": description["resource"]["uri"], "activation": activation,
            "kubernetes_ref": related["resource"]["uri"], "dashboard_url": origin, "api_origin": endpoint}


async def main_async(args):
    async with LoomClient(args.observer, token=os.environ["LOOM_GRIP_TOKEN"],
                          grip_authority=args.authority, workspace_id=args.workspace,
                          allow_insecure_grip=args.allow_http, timeout=90) as client:
        return await connect(client, package=args.package, slave=args.slave, name=args.name,
                             dashboard_url=args.dashboard_url, kubernetes_ref=args.kubernetes_ref,
                             display_name=args.display_name, api_origin=args.api_origin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", default=os.getenv("LOOM_OBSERVER_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--authority", default="loom.local")
    parser.add_argument("--workspace", default="workspace-default")
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--package", required=True)
    parser.add_argument("--slave", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--api-origin", help="Runtime API origin when production routing differs from the public Dashboard URL")
    parser.add_argument("--kubernetes-ref", required=True)
    parser.add_argument("--display-name")
    print(json.dumps(asyncio.run(main_async(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
