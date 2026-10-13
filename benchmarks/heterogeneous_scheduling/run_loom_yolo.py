#!/usr/bin/env python3
"""Run one fixed YOLO burst through Loom and collect persisted evidence."""

from __future__ import annotations

import argparse
import base64
import json
import math
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx


def mcp_call(client: httpx.Client, conversation_ref: str, name: str, arguments: dict, *, timeout: float = 30.0) -> dict:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": uuid4().hex, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers={"X-Loom-Conversation-Ref": conversation_ref},
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()["result"]
    value = json.loads((result.get("content") or [{}])[0].get("text") or "{}")
    if result.get("isError"):
        raise RuntimeError(f"{name} failed: {value}")
    return value


def put(client: httpx.Client, conversation_ref: str, content: object, media_type: str) -> dict:
    return mcp_call(client, conversation_ref, "loom_put_content", {"content": content, "media_type": media_type})["resource_ref"]


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def event_time(event: dict) -> datetime:
    return datetime.fromisoformat(str(event["created_at"]).replace("Z", "+00:00"))


def collect_metrics(run: dict, *, policy_id: str, wall_ms: float) -> dict[str, object]:
    dispatched = {event["node_id"]: event for event in run["events"] if event.get("phase") == "node_dispatched"}
    completed = {event["node_id"]: event for event in run["events"] if event.get("phase") == "node_completed"}
    node_latencies = [
        (event_time(completed[node_id]) - event_time(event)).total_seconds() * 1000
        for node_id, event in dispatched.items()
        if node_id in completed
    ]
    accepted = [event for event in run["events"] if event.get("phase") == "node_accepted"]
    decisions = [event.get("scheduling_decision") or {} for event in accepted]
    assignments = Counter(event.get("selected_target") for event in accepted)
    p95_latency = percentile(node_latencies, 0.95)
    return {
        "policy_id": policy_id,
        "run_id": run["run_id"],
        "state": run["state"],
        "jobs": len(run.get("dynamic_nodes") or []),
        "completed_jobs": len(completed),
        "wall_latency_ms": round(wall_ms, 3),
        "node_latency_mean_ms": round(sum(node_latencies) / len(node_latencies), 3) if node_latencies else None,
        "node_latency_p95_ms": round(p95_latency, 3) if p95_latency is not None else None,
        "assignments": dict(sorted(assignments.items())),
        "scheduler_overhead_mean_us": round(
            sum(float(item.get("overhead_us", 0)) for item in decisions) / len(decisions), 3
        ) if decisions else None,
        "decision_policy_ids": sorted({
            str(item["policy_id"])
            for item in decisions
            if isinstance(item.get("policy_id"), str)
        }),
        "attempts": [
            {
                "attempt_id": item.get("attempt_id"),
                "node_id": item.get("node_id"),
                "target": item.get("target"),
                "state": item.get("state"),
                "inference_ms": (item.get("value") or {}).get("inference_ms"),
            }
            for item in run.get("attempts") or []
        ],
    }


def load_requests(archive: Path, manifest_path: Path, count: int) -> list[dict[str, str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = manifest["images"][:count]
    requests = []
    with zipfile.ZipFile(archive) as source:
        for sequence, image in enumerate(selected, start=1):
            raw = source.read(f"jpgs/{image['filename']}")
            requests.append(
                {
                    "request_id": f"scheduler-{sequence:04d}",
                    "image_id": image["image_id"],
                    "image_base64": base64.b64encode(raw).decode("ascii"),
                }
            )
    return requests


def run(args: argparse.Namespace) -> dict[str, object]:
    conversation_ref = f"scheduler-yolo-{args.policy}-{uuid4().hex[:10]}"
    requests = load_requests(args.archive, args.manifest, args.jobs)
    with httpx.Client(base_url=args.observer_url) as client:
        packages = client.get("/api/v1/capability-packages", timeout=30).json()
        package = next(item for item in packages if item["package_id"] == "yolo11n-http" and item["package_version"] == "v4")
        capability_export = package["capability_exports"][0]
        package_ref = {
            "resource_id": "capability-package://yolo11n-http/v4",
            "version_or_digest": package["package_digest"],
        }
        descriptor_ref = capability_export["capability_descriptor_ref"]
        parent_input_schema = put(
            client,
            conversation_ref,
            {"type": "object", "required": ["requests"], "properties": {"requests": {"type": "array"}}},
            "application/schema+json",
        )
        parent_contract = put(
            client,
            conversation_ref,
            {"schema_version": "io.v1", "input_schema_ref": parent_input_schema, "output_schema_ref": None, "success_semantics": None, "success_validator_ref": None},
            "application/vnd.loom.io-contract+json",
        )
        orchestration_descriptor = put(
            client,
            conversation_ref,
            {"schema_version": "operation.v1", "resource_id": "loom://scheduler-yolo-burst", "name": "scheduler-yolo-burst"},
            "application/vnd.loom.operation-descriptor+json",
        )
        request_refs = [put(client, conversation_ref, request, "application/json") for request in requests]
        input_ref = put(client, conversation_ref, {"requests": request_refs}, "application/json")
        closure = {
            "closure_id": f"scheduler-yolo-{uuid4().hex[:8]}",
            "goal": "Run a fixed burst of independent YOLO detections to evaluate Loom scheduling",
            "allowed_effects": [],
            "resource_budget": {"max_nodes": args.jobs, "max_live_nodes": args.jobs},
            "recovery_policy": {"allow_reassignment": True},
            "result_expectations": [{"kind": "content", "identity_criterion": "content_digest"}],
            "body": {"closure_id": "scheduler-yolo", "program": {"operation_ref": "loom://scheduler-yolo-burst", "io_contract_ref": parent_contract}},
        }
        opened = mcp_call(client, conversation_ref, "loom_open_run", {"closure_contract": closure})
        run_id = opened["run_id"]
        mcp_call(
            client,
            conversation_ref,
            "loom_apply_plan_patch",
            {"ops": [{"kind": "set_execution_payload", "value": {"node_id": "loom://scheduler-yolo-burst", "input_ref": input_ref}}]},
        )
        source = f'''YOLO = {package_ref!r}
YOLO_DESCRIPTOR = {descriptor_ref!r}


async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":
    document = await ctx.read_json(input_ref)
    handles = [ctx.emit_node(YOLO, YOLO_DESCRIPTOR, [item]) for item in document["requests"]]
    results = [await ctx.result(handle) for handle in handles]
    return results[-1]
'''
        program_ref = put(client, conversation_ref, source, "text/x-python")
        patched = mcp_call(
            client,
            conversation_ref,
            "loom_apply_plan_patch",
            {"ops": [{"kind": "materialize_capability_package_candidate", "value": {
                "package_id": f"scheduler-yolo-orchestrator-{uuid4().hex[:8]}",
                "package_version": "v1",
                "package_type": "function",
                "execution": {"kind": "container:python_orchestrator", "version": "1"},
                "capability_exports": [{"capability_descriptor_ref": orchestration_descriptor, "io_contract_ref": parent_contract, "effect_class": "Sandboxed", "permissions": [], "replay_safety": "DeterministicByEventLog", "runtime_binding": {}}],
                "body": {"program_content_ref": program_ref, "allowed_node_package_refs": [package_ref], "max_nodes": args.jobs, "max_live_nodes": args.jobs},
            }}]},
        )
        if not patched["readiness"]["ready"]:
            raise RuntimeError(f"run not ready: {patched['readiness']['blockers']}")
        committed = mcp_call(client, conversation_ref, "loom_commit_plan", {})
        started = time.perf_counter()
        try:
            mcp_call(client, conversation_ref, "loom_start_run", {"closure_version": committed["closure_version"]}, timeout=args.timeout)
        except (httpx.HTTPError, RuntimeError):
            pass
        deadline = time.monotonic() + args.timeout
        while True:
            response = client.get(f"/api/v1/runs/{run_id}", timeout=30)
            response.raise_for_status()
            run_record = response.json()
            if run_record["state"] in {"completed", "failed", "cancelled", "awaiting_decision"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} did not finish")
            time.sleep(1)
        wall_ms = (time.perf_counter() - started) * 1000
        return collect_metrics(run_record, policy_id=args.policy, wall_ms=wall_ms)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, choices=["round_robin_v1", "least_loaded_v1", "earliest_finish_v1"])
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--observer-url", default="http://localhost:18080")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["state"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
