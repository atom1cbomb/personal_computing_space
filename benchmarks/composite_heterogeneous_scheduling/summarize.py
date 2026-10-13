#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def summarize_results(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        config = record["configuration"]
        grouped[(config["topology"], config["policy_id"])].append(record)
    cells = []
    for (topology, policy), items in sorted(grouped.items()):
        successful = [item for item in items if item["metrics"].get("state") == "completed" and (item["metrics"].get("correctness") or {}).get("complete") is True]
        walls = [float(item["metrics"]["wall_ms"]) for item in successful]
        overheads = [float(item["metrics"]["scheduler_overhead_us"]["mean"]) for item in items if (item["metrics"].get("scheduler_overhead_us") or {}).get("mean") is not None]
        cells.append({
            "topology": topology,
            "policy_id": policy,
            "runs": len(items),
            "successful_runs": len(successful),
            "success_rate": round(len(successful) / len(items), 4),
            "successful_wall_mean_ms": round(statistics.fmean(walls), 3) if walls else None,
            "successful_wall_median_ms": round(statistics.median(walls), 3) if walls else None,
            "successful_wall_p95_ms": round(_p95(walls) or 0.0, 3) if walls else None,
            "scheduler_overhead_mean_us": round(statistics.fmean(overheads), 3) if overheads else None,
        })
    return {"total_runs": len(records), "cells": cells}


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 复合异构调度实验汇总",
        "",
        "失败运行保留在成功率分母中；耗时统计只针对同时满足 Run 完成和业务输出完整的运行。",
        "",
        "| 拓扑 | 策略 | 成功/总数 | 成功率 | 平均耗时(ms) | 中位耗时(ms) | P95(ms) | 平均调度开销(μs) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["cells"]:
        def show(value: Any) -> str:
            return "—" if value is None else str(value)
        lines.append(f"| {item['topology']} | `{item['policy_id']}` | {item['successful_runs']}/{item['runs']} | {item['success_rate']:.2%} | {show(item['successful_wall_mean_ms'])} | {show(item['successful_wall_median_ms'])} | {show(item['successful_wall_p95_ms'])} | {show(item['scheduler_overhead_mean_us'])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    records = [json.loads(path.read_text(encoding="utf-8")) for path in args.inputs]
    summary = summarize_results(records)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(summary), encoding="utf-8")


if __name__ == "__main__":
    main()
