from __future__ import annotations

from benchmarks.composite_heterogeneous_scheduling.run_calibration import evaluate_calibration


def test_calibration_accepts_ordered_stable_slaves_with_sufficient_ratio() -> None:
    result = evaluate_calibration({
        "slave-a": [1000, 980, 1020, 990],
        "slave-b": [600, 590, 610, 605],
        "slave-c": [300, 295, 305, 302],
    })

    assert result["passed"] is True
    assert result["medians_ms"] == {"slave-a": 995.0, "slave-b": 602.5, "slave-c": 301.0}
    assert result["slowest_fastest_ratio"] == 3.306
    assert result["failures"] == []


def test_calibration_rejects_wrong_order_small_ratio_and_high_variation() -> None:
    result = evaluate_calibration({
        "slave-a": [500, 1500],
        "slave-b": [700, 710],
        "slave-c": [800, 810],
    })

    assert result["passed"] is False
    assert set(result["failures"]) == {"median_order_not_a_gt_b_gt_c", "slowest_fastest_ratio_below_1.5", "coefficient_of_variation_above_0.30"}

