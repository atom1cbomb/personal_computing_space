#!/usr/bin/env python3
"""Deterministic mechanism check for Loom scheduling policies.

This exercises the production policy implementations with the same generated
trace. It is deliberately labelled a simulation, not an end-to-end benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from statistics import mean

from loom_v2.driver.scheduling import SchedulingCandidate, SchedulingContext, create_scheduling_policy


PROFILES = {
    "slave-a": {"capacity": 1, "estimated_ms": 800.0, "actual_ms": 820.0},
    "slave-b": {"capacity": 4, "estimated_ms": 200.0, "actual_ms": 210.0},
}


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def run_policy(policy_id: str, *, jobs: int, seed: int, deadline_ms: float) -> dict[str, object]:
    policy = create_scheduling_policy(policy_id)
    randomizer = random.Random(seed)
    inflight = {target: 0 for target in PROFILES}
    slots = {
        target: [0.0] * int(profile["capacity"])
        for target, profile in PROFILES.items()
    }
    assignments = {target: 0 for target in PROFILES}
    latencies: list[float] = []

    for sequence in range(jobs):
        context = SchedulingContext(
            scheduling_key="controlled-burst",
            candidates=[
                SchedulingCandidate(
                    target_id=target,
                    inflight=inflight[target],
                    capacity=int(profile["capacity"]),
                    service_time_ms=float(profile["estimated_ms"]),
                    ready=True,
                )
                for target, profile in sorted(PROFILES.items())
            ],
        )
        decision = policy.select(context)
        target = decision.selected_target
        slot_index = min(range(len(slots[target])), key=slots[target].__getitem__)
        noise = randomizer.uniform(0.85, 1.15)
        duration = PROFILES[target]["actual_ms"] * noise
        completion = slots[target][slot_index] + duration
        slots[target][slot_index] = completion
        inflight[target] += 1
        assignments[target] += 1
        latencies.append(completion)

    return {
        "policy_id": policy_id,
        "jobs": jobs,
        "seed": seed,
        "deadline_ms": deadline_ms,
        "success_rate": sum(value <= deadline_ms for value in latencies) / jobs,
        "deadline_miss_rate": sum(value > deadline_ms for value in latencies) / jobs,
        "mean_latency_ms": round(mean(latencies), 3),
        "p95_latency_ms": round(percentile(latencies, 0.95), 3),
        "makespan_ms": round(max(latencies), 3),
        "assignments": assignments,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--deadline-ms", type=float, default=3000.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.jobs <= 0:
        parser.error("--jobs must be positive")

    payload = {
        "experiment": "controlled_policy_simulation_v1",
        "evidence_level": "simulation_only",
        "profiles": PROFILES,
        "results": [
            run_policy(policy_id, jobs=args.jobs, seed=args.seed, deadline_ms=args.deadline_ms)
            for policy_id in ("round_robin_v1", "least_loaded_v1", "earliest_finish_v1")
        ],
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
