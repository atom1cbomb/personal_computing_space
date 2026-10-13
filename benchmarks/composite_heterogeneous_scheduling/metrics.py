from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _distribution(values: list[float], *, unit_suffix: str = "_ms") -> dict[str, Any]:
    if not values:
        return {"count": 0, f"mean{unit_suffix}": None, f"median{unit_suffix}": None, f"p95{unit_suffix}": None, f"max{unit_suffix}": None}
    return {
        "count": len(values),
        f"mean{unit_suffix}": round(statistics.fmean(values), 3),
        f"median{unit_suffix}": round(statistics.median(values), 3),
        f"p95{unit_suffix}": round(_percentile(values, 0.95) or 0.0, 3),
        f"max{unit_suffix}": round(max(values), 3),
    }


def _package_id(node: dict[str, Any]) -> str:
    resource_id = str((node.get("package_ref") or {}).get("resource_id") or "unknown")
    coordinate = resource_id.removeprefix("capability-package://")
    return coordinate.split("/", 1)[0]


def _stage(package_id: str) -> str:
    return {
        "image-preprocess-http": "preprocess",
        "yolo11n-http": "detect",
        "inspection-assess-risk": "risk",
        "inspection-aggregate": "aggregate",
    }.get(package_id, package_id)


def collect_run_metrics(
    run: dict[str, Any],
    *,
    wall_ms: float,
    policy_id: str,
    topology: str,
    image_digest: str,
    package_digests: dict[str, str],
    final_output: dict[str, Any] | None,
    final_content_ref: dict[str, Any] | None,
) -> dict[str, Any]:
    nodes = {item["node_id"]: item for item in run.get("dynamic_nodes") or []}
    dispatched = {item["node_id"]: item for item in run.get("events") or [] if item.get("phase") == "node_dispatched"}
    completed = {item["node_id"]: item for item in run.get("events") or [] if item.get("phase") == "node_completed"}
    accepted = {item["node_id"]: item for item in run.get("events") or [] if item.get("phase") in {"node_accepted", "node_reassigned"}}

    stage_latencies: dict[str, list[float]] = defaultdict(list)
    by_slave: Counter[str] = Counter()
    by_package: Counter[str] = Counter()
    by_pair: dict[str, Counter[str]] = defaultdict(Counter)
    overheads: list[float] = []
    for node_id, node in nodes.items():
        package_id = _package_id(node)
        event = accepted.get(node_id)
        if event:
            target = str(event.get("selected_target") or event.get("target") or "unknown")
            by_slave[target] += 1
            by_package[package_id] += 1
            by_pair[target][package_id] += 1
            decision = event.get("scheduling_decision") or {}
            if decision.get("overhead_us") is not None:
                overheads.append(float(decision["overhead_us"]))
        if node_id in dispatched and node_id in completed:
            latency = (_timestamp(completed[node_id]["created_at"]) - _timestamp(dispatched[node_id]["created_at"])).total_seconds() * 1000
            stage_latencies[_stage(package_id)].append(latency)

    inference_times = [
        float(value)
        for attempt in run.get("attempts") or []
        if _stage(_package_id(nodes.get(str(attempt.get("node_id")), {}))) == "detect"
        for value in [(attempt.get("value") or {}).get("inference_ms")]
        if value is not None
    ]
    if final_output is None:
        correctness = {"complete": False, "reason": "final_output_missing"}
    else:
        correctness = {
            "complete": bool(
                final_output.get("expected_count") == final_output.get("processed_count") == final_output.get("unique_count")
                and not final_output.get("missing_image_ids")
                and not final_output.get("duplicate_image_ids")
            ),
            "expected_count": final_output.get("expected_count"),
            "processed_count": final_output.get("processed_count"),
            "unique_count": final_output.get("unique_count"),
            "missing_image_ids": final_output.get("missing_image_ids"),
            "duplicate_image_ids": final_output.get("duplicate_image_ids"),
        }
    scheduler = {
        "count": len(overheads),
        "mean": round(statistics.fmean(overheads), 3) if overheads else None,
        "p95": round(_percentile(overheads, 0.95) or 0.0, 3) if overheads else None,
    }
    return {
        "run_id": run.get("run_id"),
        "state": run.get("state"),
        "policy_id": policy_id,
        "topology": topology,
        "wall_ms": round(wall_ms, 3),
        "total_nodes": len(nodes),
        "completed_nodes": len(completed),
        "stage_metrics": {name: _distribution(stage_latencies[name]) for name in sorted(stage_latencies)},
        "assignments": {
            "by_package": dict(sorted(by_package.items())),
            "by_slave": dict(sorted(by_slave.items())),
            "by_slave_and_package": {key: dict(sorted(value.items())) for key, value in sorted(by_pair.items())},
        },
        "scheduler_overhead_us": scheduler,
        "yolo_inference_ms": _distribution(inference_times, unit_suffix=""),
        "correctness": correctness,
        "image_digest": image_digest,
        "package_digests": dict(sorted(package_digests.items())),
        "final_content_ref": final_content_ref,
    }
