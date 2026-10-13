import pytest

from loom_v2.coding_agents.fake import FakeCodingAgentProvider
from loom_v2.driver.service import DriverService
from loom_v2.driver.scheduling import (
    SchedulingCandidate,
    SchedulingContext,
    create_scheduling_policy,
)
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings


def _context(*candidates: SchedulingCandidate) -> SchedulingContext:
    return SchedulingContext(
        scheduling_key="package-a#descriptor-a",
        candidates=list(candidates),
    )


def test_round_robin_policy_preserves_ready_first_rotation():
    policy = create_scheduling_policy("round_robin_v1")
    context = _context(
        SchedulingCandidate(target_id="slave-a", ready=True),
        SchedulingCandidate(target_id="slave-b", ready=True),
        SchedulingCandidate(target_id="slave-c", ready=False),
    )

    assert [policy.select(context).selected_target for _ in range(3)] == [
        "slave-a",
        "slave-b",
        "slave-a",
    ]


def test_least_loaded_policy_compares_load_relative_to_capacity():
    policy = create_scheduling_policy("least_loaded_v1")
    context = _context(
        SchedulingCandidate(target_id="slave-a", inflight=1, capacity=1),
        SchedulingCandidate(target_id="slave-b", inflight=2, capacity=4),
    )

    decision = policy.select(context)

    assert decision.selected_target == "slave-b"
    assert decision.scores == {"slave-a": 1.0, "slave-b": 0.5}


def test_earliest_finish_accounts_for_capacity_service_time_and_cold_start():
    policy = create_scheduling_policy("earliest_finish_v1")
    context = _context(
        SchedulingCandidate(
            target_id="slave-a",
            inflight=0,
            capacity=1,
            service_time_ms=800,
            cold_start_ms=0,
            ready=True,
        ),
        SchedulingCandidate(
            target_id="slave-b",
            inflight=2,
            capacity=4,
            service_time_ms=200,
            cold_start_ms=300,
            ready=False,
        ),
    )

    decision = policy.select(context)

    assert decision.selected_target == "slave-b"
    assert decision.scores == {"slave-a": 800.0, "slave-b": 500.0}
    assert decision.reason == "minimum_expected_finish_ms"


def test_policy_ties_are_resolved_by_target_id():
    policy = create_scheduling_policy("earliest_finish_v1")
    context = _context(
        SchedulingCandidate(target_id="slave-b", service_time_ms=100),
        SchedulingCandidate(target_id="slave-a", service_time_ms=100),
    )

    assert policy.select(context).selected_target == "slave-a"


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError, match="unknown_scheduling_policy"):
        create_scheduling_policy("magic")


def test_driver_builds_configured_policy_from_settings():
    service = DriverService(
        ObserverRepository(),
        FakeCodingAgentProvider(),
        settings=Settings(scheduling_policy="least_loaded_v1"),
    )

    assert service.scheduling_policy.policy_id == "least_loaded_v1"


def test_slave_scheduling_profile_settings_are_positive():
    settings = Settings(
        scheduling_capacity=4,
        scheduling_service_time_ms=250,
        scheduling_cold_start_ms=800,
    )

    assert settings.scheduling_capacity == 4
    with pytest.raises(ValueError):
        Settings(scheduling_capacity=0)


def test_slave_parses_default_and_per_package_scheduling_profiles(monkeypatch):
    monkeypatch.setenv(
        "LOOM_SCHEDULING_PROFILES",
        '{"default":{"capacity":2,"service_time_ms":500,"cold_start_ms":50},'
        '"packages":{"yolo11n-http":{"capacity":1,"service_time_ms":200,"cold_start_ms":1500}}}',
    )

    settings = Settings(_env_file=None)

    assert settings.scheduling_profiles.default.capacity == 2
    assert settings.scheduling_profiles.packages["yolo11n-http"].service_time_ms == 200
    assert settings.scheduling_profiles.packages["yolo11n-http"].cold_start_ms == 1500
