from __future__ import annotations

from benchmarks.composite_heterogeneous_scheduling.metrics import collect_run_metrics


def test_metrics_join_nodes_events_and_attempts_by_node_id() -> None:
    run = {
        "run_id": "run-1",
        "state": "completed",
        "dynamic_nodes": [
            {"node_id": "n1", "package_ref": {"resource_id": "capability-package://image-preprocess-http/v1"}, "state": "completed"},
            {"node_id": "n2", "package_ref": {"resource_id": "capability-package://yolo11n-http/v4"}, "state": "completed"},
        ],
        "events": [
            {"phase": "node_accepted", "node_id": "n1", "selected_target": "slave-a", "created_at": "2026-10-13T00:00:00.000000+00:00", "scheduling_decision": {"policy_id": "earliest_finish_v1", "overhead_us": 10}},
            {"phase": "node_dispatched", "node_id": "n1", "created_at": "2026-10-13T00:00:00.010000+00:00"},
            {"phase": "node_completed", "node_id": "n1", "created_at": "2026-10-13T00:00:00.110000+00:00"},
            {"phase": "node_accepted", "node_id": "n2", "selected_target": "slave-b", "created_at": "2026-10-13T00:00:00.110000+00:00", "scheduling_decision": {"policy_id": "earliest_finish_v1", "overhead_us": 30}},
            {"phase": "node_dispatched", "node_id": "n2", "created_at": "2026-10-13T00:00:00.130000+00:00"},
            {"phase": "node_completed", "node_id": "n2", "created_at": "2026-10-13T00:00:00.330000+00:00"},
        ],
        "attempts": [
            {"node_id": "n1", "target": "slave-a", "state": "completed", "value": {"image_id": "i1"}},
            {"node_id": "n2", "target": "slave-b", "state": "completed", "value": {"image_id": "i1", "inference_ms": 180.0}},
        ],
    }

    metrics = collect_run_metrics(
        run,
        wall_ms=350.0,
        policy_id="earliest_finish_v1",
        topology="a-b-c",
        image_digest="images-sha",
        package_digests={"detect": "package-sha"},
        final_output={"expected_count": 1, "processed_count": 1, "unique_count": 1, "missing_image_ids": [], "duplicate_image_ids": []},
        final_content_ref={"resource_id": "content://sha256/final"},
    )

    assert metrics["completed_nodes"] == 2
    assert metrics["stage_metrics"]["preprocess"] == {
        "count": 1,
        "mean_ms": 100.0,
        "median_ms": 100.0,
        "p95_ms": 100.0,
        "max_ms": 100.0,
    }
    assert metrics["stage_metrics"]["detect"]["mean_ms"] == 200.0
    assert metrics["assignments"] == {
        "by_package": {"image-preprocess-http": 1, "yolo11n-http": 1},
        "by_slave": {"slave-a": 1, "slave-b": 1},
        "by_slave_and_package": {"slave-a": {"image-preprocess-http": 1}, "slave-b": {"yolo11n-http": 1}},
    }
    assert metrics["scheduler_overhead_us"] == {"count": 2, "mean": 20.0, "p95": 30.0}
    assert metrics["yolo_inference_ms"] == {"count": 1, "mean": 180.0, "median": 180.0, "p95": 180.0, "max": 180.0}
    assert metrics["correctness"]["complete"] is True
    assert metrics["image_digest"] == "images-sha"
    assert metrics["package_digests"] == {"detect": "package-sha"}


def test_metrics_preserve_failed_run_and_missing_nodes() -> None:
    metrics = collect_run_metrics(
        {"run_id": "run-failed", "state": "failed", "dynamic_nodes": [], "events": [], "attempts": []},
        wall_ms=12.5,
        policy_id="round_robin_v1",
        topology="a-b",
        image_digest="d",
        package_digests={},
        final_output=None,
        final_content_ref=None,
    )

    assert metrics["state"] == "failed"
    assert metrics["correctness"] == {"complete": False, "reason": "final_output_missing"}
