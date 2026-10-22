"""Register a Grafana resource and activate its package through Observer GRIP."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re

from capability_services.grafana_http.server import gpu_metric_config, https_origin, uid
from loom_orchestration import LoomClient

ROOT = Path(__file__).resolve().parents[1]


async def connect(client: LoomClient, *, package: str, slave: str, name: str,
                  dashboard_url: str, kubernetes_ref: str, api_origin: str,
                  credential_profile: str | None = None, volcano_ref: str | None = None,
                  datasource_uid: str | None = None, gpu_metric: dict | None = None,
                  display_name: str | None = None):
    public_origin, endpoint = https_origin(dashboard_url), https_origin(api_origin)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise ValueError("instance_name_invalid")
    related = await client.describe(kubernetes_ref)
    if related["resource"]["mcue"] != "capability/activation":
        raise ValueError("kubernetes_activation_required")
    kubernetes = await client.invoke(related["resource"]["metadata"]["package"], client.names.action("read"), {})
    if kubernetes["package_id"] != "kubernetes-http":
        raise ValueError("kubernetes_activation_required")
    published = await client.invoke(package, client.names.action("read"), {})
    if published["package_id"] != "grafana-http":
        raise ValueError("grafana_package_required")
    attributes = {"display_name": display_name or name + " Grafana", "dashboard_url": public_origin,
                  "description": "Grafana monitoring, GPU identity and actual GPU utilization.",
                  "kubernetes_ref": related["resource"]["uri"]}
    if volcano_ref is not None:
        volcano = (await client.describe(volcano_ref))["resource"]
        if (volcano["mcue"] != "computing/volcano"
                or volcano["metadata"]["attributes"]["kubernetes_ref"] != related["resource"]["uri"]):
            raise ValueError("volcano_kubernetes_binding_mismatch")
        attributes["volcano_ref"] = volcano["uri"]
    configuration = {"api_origin": endpoint, "browser_origin": public_origin}
    if datasource_uid is not None:
        configuration["datasource_uid"] = uid(datasource_uid)
    if gpu_metric is not None:
        configuration["gpu_metric"] = gpu_metric_config(gpu_metric)
    definition = json.loads((ROOT / "loom_v2/mcue_types/computing-grafana-v1.json").read_text())
    mcue = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), definition)
    requested = client.names.name("resources", "grafana", name)
    existing = next((x for x in await client.resources(mcue="computing/grafana") if x["uri"] == requested), None)
    if existing is None:
        registered = await client.invoke(client.names.root, client.names.action("register-resource"), {
            "resource_id": requested, "mcue_ref": mcue["resource"], "attributes": attributes,
            "package": package, "state": "registered",
        })
        target = registered["resource"]
    else:
        target = existing["uri"]
    activation = await client.invoke(slave, client.names.action("activate-package"), {
        "package": package, "instance_name": name, "target": target,
        "instance_config": configuration, "credential_profile": credential_profile,
        "idempotency_key": f"connect-grafana-{name}-{published['package_digest'][:16]}",
    })
    if existing is not None:
        before = (await client.describe(target))["resource"]["metadata"]
        if any(before["attributes"].get(key) != value for key, value in attributes.items()):
            await client.invoke(target, client.names.action("update-attributes"), {
                "expected_attributes_revision": before["attributes_revision"], "patch": attributes,
            })
    return {"resource": target, "activation": activation,
            "kubernetes_ref": related["resource"]["uri"], "dashboard_url": public_origin}


async def main_async(args):
    configuration = json.loads(args.query_config.read_text()) if args.query_config else {}
    if set(configuration) - {"datasource_uid", "gpu_metric"}:
        raise ValueError("query_config_invalid")
    async with LoomClient(args.observer, token=os.environ["LOOM_GRIP_TOKEN"],
                          grip_authority=args.authority, workspace_id=args.workspace,
                          allow_insecure_grip=args.allow_http, timeout=90) as client:
        return await connect(client, package=args.package, slave=args.slave, name=args.name,
                             dashboard_url=args.dashboard_url, api_origin=args.api_origin,
                             kubernetes_ref=args.kubernetes_ref, volcano_ref=args.volcano_ref,
                             credential_profile=args.credential_profile, display_name=args.display_name,
                             **configuration)


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
    parser.add_argument("--api-origin", required=True)
    parser.add_argument("--kubernetes-ref", required=True)
    parser.add_argument("--volcano-ref")
    parser.add_argument("--credential-profile")
    parser.add_argument("--query-config", type=Path, help="JSON with verified datasource_uid and gpu_metric")
    parser.add_argument("--display-name")
    print(json.dumps(asyncio.run(main_async(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
