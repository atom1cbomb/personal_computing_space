from benchmarks.heterogeneous_scheduling.run_policy_simulation import run_policy
from benchmarks.heterogeneous_scheduling.run_loom_yolo import collect_metrics


def test_earliest_finish_improves_tail_latency_on_controlled_heterogeneous_trace():
    baseline = run_policy("round_robin_v1", jobs=40, seed=20261013, deadline_ms=3000)
    scheduled = run_policy("earliest_finish_v1", jobs=40, seed=20261013, deadline_ms=3000)

    assert scheduled["p95_latency_ms"] < baseline["p95_latency_ms"]
    assert scheduled["deadline_miss_rate"] < baseline["deadline_miss_rate"]
    assert scheduled["assignments"] != baseline["assignments"]


def test_collect_metrics_uses_persisted_node_events():
    run = {
        "run_id": "run-1",
        "state": "completed",
        "dynamic_nodes": [{"node_id": "node-1"}],
        "attempts": [{"node_id": "node-1", "target": "slave-b", "state": "completed"}],
        "events": [
            {"phase": "node_accepted", "node_id": "node-1", "selected_target": "slave-b", "created_at": "2026-10-13T00:00:00+00:00", "scheduling_decision": {"policy_id": "earliest_finish_v1", "overhead_us": 12.5}},
            {"phase": "node_dispatched", "node_id": "node-1", "created_at": "2026-10-13T00:00:00+00:00"},
            {"phase": "node_completed", "node_id": "node-1", "created_at": "2026-10-13T00:00:00.250000+00:00"},
        ],
    }

    result = collect_metrics(run, policy_id="earliest_finish_v1", wall_ms=300)

    assert result["node_latency_p95_ms"] == 250
    assert result["assignments"] == {"slave-b": 1}
    assert result["scheduler_overhead_mean_us"] == 12.5
