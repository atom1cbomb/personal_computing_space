from __future__ import annotations

from benchmarks.composite_heterogeneous_scheduling.summarize import summarize_results


def test_summary_keeps_failed_runs_in_success_rate_denominator() -> None:
    records = [
        {"configuration": {"topology": "a-b", "policy_id": "round_robin_v1"}, "metrics": {"state": "completed", "wall_ms": 1000, "correctness": {"complete": True}, "scheduler_overhead_us": {"mean": 10}}},
        {"configuration": {"topology": "a-b", "policy_id": "round_robin_v1"}, "metrics": {"state": "failed", "wall_ms": 400, "correctness": {"complete": False}, "scheduler_overhead_us": {"mean": 12}}},
        {"configuration": {"topology": "a-b", "policy_id": "round_robin_v1"}, "metrics": {"state": "completed", "wall_ms": 800, "correctness": {"complete": True}, "scheduler_overhead_us": {"mean": 8}}},
    ]

    summary = summarize_results(records)

    assert summary["cells"][0] == {
        "topology": "a-b",
        "policy_id": "round_robin_v1",
        "runs": 3,
        "successful_runs": 2,
        "success_rate": 0.6667,
        "successful_wall_mean_ms": 900.0,
        "successful_wall_median_ms": 900.0,
        "successful_wall_p95_ms": 1000.0,
        "scheduler_overhead_mean_us": 10.0,
    }


def test_summary_groups_each_topology_and_policy_cell() -> None:
    records = [
        {"configuration": {"topology": "a-b-c", "policy_id": "least_loaded_v1"}, "metrics": {"state": "failed", "wall_ms": 1, "correctness": {"complete": False}, "scheduler_overhead_us": {"mean": None}}},
        {"configuration": {"topology": "a-b", "policy_id": "earliest_finish_v1"}, "metrics": {"state": "completed", "wall_ms": 2, "correctness": {"complete": True}, "scheduler_overhead_us": {"mean": 5}}},
    ]

    summary = summarize_results(records)

    assert [(item["topology"], item["policy_id"]) for item in summary["cells"]] == [
        ("a-b", "earliest_finish_v1"),
        ("a-b-c", "least_loaded_v1"),
    ]
    assert summary["cells"][1]["success_rate"] == 0.0
    assert summary["cells"][1]["successful_wall_mean_ms"] is None
