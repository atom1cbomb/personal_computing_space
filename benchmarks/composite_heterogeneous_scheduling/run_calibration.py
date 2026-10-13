#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from benchmarks.composite_heterogeneous_scheduling.run_composite import run_experiment


def evaluate_calibration(samples: dict[str, list[float]]) -> dict[str, Any]:
    required = ("slave-a", "slave-b", "slave-c")
    if any(not samples.get(name) for name in required):
        return {"passed": False, "medians_ms": {}, "coefficient_of_variation": {}, "slowest_fastest_ratio": None, "failures": ["samples_missing"]}
    medians = {name: round(statistics.median(samples[name]), 3) for name in required}
    variation = {
        name: round(statistics.pstdev(values) / statistics.fmean(values), 4) if statistics.fmean(values) else 0.0
        for name, values in samples.items()
    }
    ratio = medians["slave-a"] / medians["slave-c"] if medians["slave-c"] else float("inf")
    failures = []
    if not medians["slave-a"] > medians["slave-b"] > medians["slave-c"]:
        failures.append("median_order_not_a_gt_b_gt_c")
    if ratio < 1.5:
        failures.append("slowest_fastest_ratio_below_1.5")
    if any(value > 0.30 for value in variation.values()):
        failures.append("coefficient_of_variation_above_0.30")
    return {
        "passed": not failures,
        "medians_ms": medians,
        "coefficient_of_variation": variation,
        "slowest_fastest_ratio": round(ratio, 3),
        "failures": failures,
    }


def run_calibration(args: argparse.Namespace) -> dict[str, Any]:
    samples: dict[str, list[float]] = {name: [] for name in ("slave-a", "slave-b", "slave-c")}
    raw: dict[str, list[dict[str, Any]]] = {name: [] for name in samples}
    for slave_id in samples:
        for index in range(args.warmups + args.measurements):
            run_args = SimpleNamespace(
                archive=args.archive,
                manifest=args.manifest,
                images=1,
                observer_url=args.observer_url,
                timeout=args.timeout,
                poll_interval=0.25,
                policy="earliest_finish_v1",
                topology="a-b-c",
                target_slave=slave_id,
            )
            record = run_experiment(run_args)
            raw[slave_id].append(record)
            metric = record["metrics"]["yolo_inference_ms"]["median"]
            if index >= args.warmups and record["metrics"]["state"] == "completed" and metric is not None:
                samples[slave_id].append(float(metric))
    return {"configuration": {"warmups": args.warmups, "measurements": args.measurements, "metric": "YOLO inference_ms from pinned Loom Attempts"}, "samples_ms": samples, "evaluation": evaluate_calibration(samples), "raw_runs": raw}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observer-url", default="http://localhost:18080")
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--measurements", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    result = run_calibration(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["evaluation"], indent=2, sort_keys=True))
    if not result["evaluation"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
