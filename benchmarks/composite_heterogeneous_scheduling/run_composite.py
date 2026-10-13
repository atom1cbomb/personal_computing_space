#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from benchmarks.composite_heterogeneous_scheduling.contracts import (
    AGGREGATE_INPUT_SCHEMA,
    AGGREGATE_OUTPUT_SCHEMA,
    RISK_INPUT_SCHEMA,
    RISK_OUTPUT_SCHEMA,
    io_contract,
)
from benchmarks.composite_heterogeneous_scheduling.metrics import collect_run_metrics
from benchmarks.composite_heterogeneous_scheduling.programs import AGGREGATE_PROGRAM, RISK_PROGRAM


PackageBinding = tuple[dict[str, Any], dict[str, Any]]


def node_limits(image_count: int) -> dict[str, int]:
    if image_count <= 0:
        raise ValueError("image_count_must_be_positive")
    return {"max_nodes": 3 * image_count + 1, "max_live_nodes": image_count}


def locality_compute_spec(slave_id: str) -> dict[str, Any]:
    return {
        "operation_ref": "loom://composite-inspection",
        "requirements": [{"key": "loom.data.locality.v1", "value": slave_id, "view": "systems"}],
    }


def build_orchestration_source(packages: dict[str, PackageBinding]) -> str:
    def plain(value: Any) -> Any:
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    constants = "\n".join(
        f"{name.upper()}_PACKAGE = {plain(binding[0])!r}\n{name.upper()}_DESCRIPTOR = {plain(binding[1])!r}"
        for name, binding in packages.items()
    )
    return f'''{constants}

async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":
    document = await ctx.read_json(input_ref)
    request_refs = document["request_refs"]
    preprocess_handles = [ctx.emit_node(PREPROCESS_PACKAGE, PREPROCESS_DESCRIPTOR, [item]) for item in request_refs]
    preprocess_refs = [await ctx.result(handle) for handle in preprocess_handles]
    detect_handles = [ctx.emit_node(DETECT_PACKAGE, DETECT_DESCRIPTOR, [item]) for item in preprocess_refs]
    detect_refs = [await ctx.result(handle) for handle in detect_handles]
    risk_handles = [ctx.emit_node(RISK_PACKAGE, RISK_DESCRIPTOR, [item]) for item in detect_refs]
    risk_refs = [await ctx.result(handle) for handle in risk_handles]
    aggregate_handle = ctx.emit_node(AGGREGATE_PACKAGE, AGGREGATE_DESCRIPTOR, [document["expected_ref"]] + risk_refs)
    return await ctx.result(aggregate_handle)
'''


def mcp_call(client: httpx.Client, conversation_ref: str, name: str, arguments: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
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


def put(client: httpx.Client, conversation_ref: str, content: object, media_type: str) -> dict[str, Any]:
    return mcp_call(client, conversation_ref, "loom_put_content", {"content": content, "media_type": media_type})["resource_ref"]


def load_requests(archive: Path, manifest_path: Path, count: int) -> tuple[list[dict[str, Any]], str, list[str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = manifest["images"][:count]
    if len(selected) != count:
        raise ValueError(f"requested {count} images but manifest contains {len(selected)}")
    requests: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive) as source:
        for sequence, image in enumerate(selected, start=1):
            raw = source.read(f"jpgs/{image['filename']}")
            requests.append({
                "request_id": f"inspection-{sequence:04d}",
                "image_id": str(image["image_id"]),
                "image_base64": base64.b64encode(raw).decode("ascii"),
                "max_edge": 1280,
                "jpeg_quality": 85,
            })
    image_ids = [item["image_id"] for item in requests]
    digest = hashlib.sha256(json.dumps(image_ids, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return requests, digest, image_ids


def _package_ref(package: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_id": f"capability-package://{package['package_id']}/{package['package_version']}",
        "version_or_digest": package["package_digest"],
    }


def _descriptor(client: httpx.Client, conversation: str, name: str) -> dict[str, Any]:
    return put(client, conversation, {"schema_version": "operation.v1", "resource_id": f"loom://{name}", "name": name}, "application/vnd.loom.operation-descriptor+json")


def _contract(client: httpx.Client, conversation: str, input_schema: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, Any]:
    input_ref = put(client, conversation, input_schema, "application/schema+json")
    output_ref = put(client, conversation, output_schema, "application/schema+json")
    return put(client, conversation, io_contract(input_ref, output_ref), "application/vnd.loom.io-contract+json")


def _process_candidate(package_id: str, descriptor: dict[str, Any], contract: dict[str, Any], program_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_id": package_id,
        "package_version": "v1",
        "package_type": "function",
        "execution": {"kind": "process:json_stdio", "version": "1"},
        "capability_exports": [{"capability_descriptor_ref": descriptor, "io_contract_ref": contract, "effect_class": "Sandboxed", "permissions": [], "replay_safety": "DeclaredByPackage", "runtime_binding": {}}],
        "body": {"program_content_ref": program_ref},
    }


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    requests, image_digest, image_ids = load_requests(args.archive, args.manifest, args.images)
    conversation = f"composite-{args.policy}-{uuid4().hex[:10]}"
    with httpx.Client(base_url=args.observer_url) as client:
        available = client.get("/api/v1/capability-packages", timeout=30).json()
        preprocess = next(item for item in available if item["package_id"] == "image-preprocess-http")
        detect = next(item for item in available if item["package_id"] == "yolo11n-http")
        bindings: dict[str, PackageBinding] = {
            "preprocess": (_package_ref(preprocess), preprocess["capability_exports"][0]["capability_descriptor_ref"]),
            "detect": (_package_ref(detect), detect["capability_exports"][0]["capability_descriptor_ref"]),
        }
        request_refs = [put(client, conversation, item, "application/json") for item in requests]
        expected_ref = put(client, conversation, {"expected_image_ids": image_ids}, "application/json")
        parent_input = put(client, conversation, {"request_refs": request_refs, "expected_ref": expected_ref}, "application/json")
        parent_contract = _contract(client, conversation, {"type": "object", "required": ["request_refs", "expected_ref"]}, AGGREGATE_OUTPUT_SCHEMA)
        orchestration_descriptor = _descriptor(client, conversation, "composite-inspection")
        limits = node_limits(args.images)
        closure = {
            "closure_id": f"composite-inspection-{uuid4().hex[:8]}",
            "goal": "Deterministic preprocess, detection, risk assessment and aggregation benchmark",
            "allowed_effects": [],
            "resource_budget": limits,
            "recovery_policy": {"allow_reassignment": True},
            "result_expectations": [{"kind": "content", "identity_criterion": "content_digest"}],
            "body": {"closure_id": "composite-inspection", "program": {"operation_ref": "loom://composite-inspection", "io_contract_ref": parent_contract}},
        }
        run_id = mcp_call(client, conversation, "loom_open_run", {"closure_contract": closure})["run_id"]
        initial_ops = [{"kind": "set_execution_payload", "value": {"node_id": "loom://composite-inspection", "input_ref": parent_input}}]
        if getattr(args, "target_slave", None):
            initial_ops.append({"kind": "set_compute_spec", "value": locality_compute_spec(args.target_slave)})
        mcp_call(client, conversation, "loom_apply_plan_patch", {"ops": initial_ops})

        process_defs = [
            ("risk", "inspection-assess-risk", RISK_INPUT_SCHEMA, RISK_OUTPUT_SCHEMA, RISK_PROGRAM),
            ("aggregate", "inspection-aggregate", AGGREGATE_INPUT_SCHEMA, AGGREGATE_OUTPUT_SCHEMA, AGGREGATE_PROGRAM),
        ]
        candidates = []
        descriptors: dict[str, dict[str, Any]] = {}
        for key, package_id, input_schema, output_schema, program in process_defs:
            descriptors[key] = _descriptor(client, conversation, package_id)
            contract = _contract(client, conversation, input_schema, output_schema)
            program_ref = put(client, conversation, program, "text/x-python")
            candidates.append({"kind": "materialize_capability_package_candidate", "value": _process_candidate(package_id, descriptors[key], contract, program_ref)})
        mcp_call(client, conversation, "loom_apply_plan_patch", {"ops": candidates})
        run_packages = mcp_call(client, conversation, "loom_list_run_capability_packages", {})["packages"]
        for key, package_id, *_ in process_defs:
            item = next(package for package in run_packages if package["package_id"] == package_id)
            bindings[key] = (_package_ref(item), descriptors[key])

        source_ref = put(client, conversation, build_orchestration_source(bindings), "text/x-python")
        orchestration = {
            "package_id": f"composite-inspection-orchestrator-{uuid4().hex[:8]}",
            "package_version": "v1",
            "package_type": "function",
            "execution": {"kind": "container:python_orchestrator", "version": "1"},
            "capability_exports": [{"capability_descriptor_ref": orchestration_descriptor, "io_contract_ref": parent_contract, "effect_class": "Sandboxed", "permissions": [], "replay_safety": "DeterministicByEventLog", "runtime_binding": {}}],
            "body": {"program_content_ref": source_ref, "allowed_node_package_refs": [value[0] for value in bindings.values()], **limits},
        }
        patched = mcp_call(client, conversation, "loom_apply_plan_patch", {"ops": [{"kind": "materialize_capability_package_candidate", "value": orchestration}]})
        if not patched["readiness"]["ready"]:
            raise RuntimeError(f"run not ready: {patched['readiness']['blockers']}")
        committed = mcp_call(client, conversation, "loom_commit_plan", {})
        started = time.perf_counter()
        try:
            mcp_call(client, conversation, "loom_start_run", {"closure_version": committed["closure_version"]}, timeout=args.timeout)
        except (httpx.HTTPError, RuntimeError):
            pass
        deadline = time.monotonic() + args.timeout
        while True:
            response = client.get(f"/api/v1/runs/{run_id}", timeout=30)
            response.raise_for_status()
            record = response.json()
            if record["state"] in {"completed", "failed", "cancelled", "awaiting_decision"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} did not finish within {args.timeout}s")
            time.sleep(args.poll_interval)
        wall_ms = (time.perf_counter() - started) * 1000
        final_ref = (record.get("outcome") or {}).get("resource_ref")
        final_output = None
        if final_ref:
            digest = final_ref["resource_id"].rsplit("/", 1)[-1]
            final_output = client.get(f"/api/v1/content/{digest}", timeout=30).json()
        package_digests = {name: ref[0]["version_or_digest"] for name, ref in bindings.items()}
        metrics = collect_run_metrics(record, wall_ms=wall_ms, policy_id=args.policy, topology=args.topology, image_digest=image_digest, package_digests=package_digests, final_output=final_output, final_content_ref=final_ref)
        return {"configuration": {"images": args.images, "policy_id": args.policy, "topology": args.topology, "target_slave": getattr(args, "target_slave", None)}, "metrics": metrics, "task_output": final_output, "run": record}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, choices=["round_robin_v1", "least_loaded_v1", "earliest_finish_v1"])
    parser.add_argument("--topology", required=True, choices=["a-b", "a-b-c"])
    parser.add_argument("--target-slave", choices=["slave-a", "slave-b", "slave-c"])
    parser.add_argument("--images", type=int, default=3)
    parser.add_argument("--observer-url", default="http://localhost:18080")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_experiment(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))
    if result["metrics"]["state"] != "completed" or not result["metrics"]["correctness"]["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
