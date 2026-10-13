from benchmarks.heterogeneous_scheduling.run_policy_simulation import run_policy


def test_earliest_finish_improves_tail_latency_on_controlled_heterogeneous_trace():
    baseline = run_policy("round_robin_v1", jobs=40, seed=20261013, deadline_ms=3000)
    scheduled = run_policy("earliest_finish_v1", jobs=40, seed=20261013, deadline_ms=3000)

    assert scheduled["p95_latency_ms"] < baseline["p95_latency_ms"]
    assert scheduled["deadline_miss_rate"] < baseline["deadline_miss_rate"]
    assert scheduled["assignments"] != baseline["assignments"]
