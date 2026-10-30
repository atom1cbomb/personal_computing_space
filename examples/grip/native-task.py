"""Publish a doubling function and submit a complete native orchestration plan."""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from loom_orchestration import (
    LoomClient, OrchestratorPackage, ResourceRef, SubmissionBuilder, TaskClosure,
    publish_function,
)


async def prepare_plan(client, request_id, value=21):
    schema = {"type": "object", "properties": {"value": {"type": "integer"}},
              "required": ["value"], "additionalProperties": False}
    worker = await publish_function(
        client, package_id="grip-native-double", package_version="v1", action_name="double",
        source='import json, sys\nprint(json.dumps({"value": json.load(sys.stdin)["value"] * 2}))\n',
        input_schema=schema, output_schema=schema,
    )
    worker_ref = ResourceRef(resource_id=worker["resource"], version_or_digest=worker["package_digest"])
    export, = worker["action_exports"]
    source = (
        'async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":\n'
        f'    handle = ctx.emit_node("double", {worker_ref.model_dump(mode="json")!r}, '
        f'{export["action_descriptor_ref"]!r}, [input_ref])\n'
        '    return await ctx.result(handle)\n'
    )
    program = await client.upload_content(source, media_type="text/x-python")
    io_ref = ResourceRef.model_validate(export["io_contract_ref"])
    action = client.names.name("actions", "grip-native-plan", "orchestrate")
    descriptor = await client.upload_content(
        {"schema_version": "action.v1", "resource_id": action, "name": "orchestrate"},
        media_type="application/vnd.loom.action-descriptor+json",
    )
    published = await client.register_package(OrchestratorPackage(
        package_id="grip-native-plan", package_version="v1", program_content_ref=program,
        allowed_node_package_refs=[worker_ref], action_descriptor_ref=descriptor,
        io_contract_ref=io_ref, max_nodes=1, max_live_nodes=1,
    ).manifest())
    description = await client.describe(published["resource"])
    orchestrate, = description["resource"]["metadata"]["actions"]
    return SubmissionBuilder(
        goal="Double the input integer", workspace_id=client.workspace_id,
        body=TaskClosure(program={"action_ref": orchestrate["uri"],
                                  "io_contract_ref": io_ref.model_dump(mode="json")}),
        orchestrator_package=ResourceRef(resource_id=published["resource"],
                                          version_or_digest=published["package_digest"]),
        input_binding=await client.upload_content({"value": value}),
        resource_budget={"max_nodes": 1, "max_live_nodes": 1, "execution_timeout_seconds": 120},
    ).build(request_id=request_id)


async def main(args):
    async with LoomClient(args.observer, token=os.environ["LOOM_GRIP_TOKEN"],
                          grip_authority=args.authority, workspace_id=args.workspace,
                          allow_insecure_grip=args.allow_http) as client:
        driver = next(r for r in await client.resources(mcue="computing/driver")
                      if r["uri"] == client.names.name("drivers", "default"))
        plan = await prepare_plan(client, args.request_id, args.value)
        receipt = await client.submit(request_id=args.request_id, plan=plan)
        frozen = await client.invoke(receipt["plan"], client.names.action("read"), {})
        state = await client.invoke(receipt["run"], client.names.action("read"), {})
        for _ in range(args.wait):
            if state["state"] in {"completed", "failed", "cancelled", "closed", "awaiting_decision", "awaiting_auth"}:
                break
            await asyncio.sleep(1)
            state = await client.invoke(receipt["run"], client.names.action("read"), {})
        return {"driver": driver["uri"], "driver_available": driver["metadata"]["available"],
                "submission": receipt, "frozen_plan": frozen, "state": state}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", default=os.getenv("LOOM_OBSERVER_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--authority", default=os.getenv("LOOM_GRIP_AUTHORITY", "loom.local"))
    parser.add_argument("--workspace", default=os.getenv("LOOM_WORKSPACE_ID", "workspace-default"))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--request-id", required=True, help="stable business idempotency key; reuse for an unchanged retry")
    parser.add_argument("--value", type=int, default=21)
    parser.add_argument("--wait", type=int, default=0, help="maximum seconds to poll; zero returns after submission")
    print(json.dumps(asyncio.run(main(parser.parse_args())), ensure_ascii=False, indent=2))
