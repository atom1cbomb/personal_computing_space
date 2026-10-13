from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SchedulingCandidate:
    target_id: str
    inflight: int = 0
    capacity: int = 1
    service_time_ms: float = 1000.0
    cold_start_ms: float = 0.0
    ready: bool = False


@dataclass(frozen=True)
class SchedulingContext:
    scheduling_key: str
    candidates: list[SchedulingCandidate]


@dataclass(frozen=True)
class SchedulingDecision:
    policy_id: str
    selected_target: str
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""


class SchedulingPolicy(Protocol):
    policy_id: str

    def select(self, context: SchedulingContext) -> SchedulingDecision:
        ...


def _require_candidates(context: SchedulingContext) -> None:
    if not context.candidates:
        raise ValueError("scheduling_candidates_required")


class RoundRobinPolicy:
    policy_id = "round_robin_v1"

    def __init__(self) -> None:
        self._cursors: dict[str, int] = {}

    def select(self, context: SchedulingContext) -> SchedulingDecision:
        _require_candidates(context)
        ready = [candidate for candidate in context.candidates if candidate.ready]
        candidates = sorted(ready or context.candidates, key=lambda item: item.target_id)
        cursor = self._cursors.get(context.scheduling_key, 0)
        selected = candidates[cursor % len(candidates)]
        self._cursors[context.scheduling_key] = cursor + 1
        return SchedulingDecision(
            policy_id=self.policy_id,
            selected_target=selected.target_id,
            reason="round_robin_ready_first",
        )


class LeastLoadedPolicy:
    policy_id = "least_loaded_v1"

    def select(self, context: SchedulingContext) -> SchedulingDecision:
        _require_candidates(context)
        scores = {
            candidate.target_id: candidate.inflight / max(1, candidate.capacity)
            for candidate in context.candidates
        }
        selected = min(context.candidates, key=lambda item: (scores[item.target_id], item.target_id))
        return SchedulingDecision(
            policy_id=self.policy_id,
            selected_target=selected.target_id,
            scores=scores,
            reason="minimum_normalized_inflight",
        )


class EarliestFinishPolicy:
    policy_id = "earliest_finish_v1"

    def select(self, context: SchedulingContext) -> SchedulingDecision:
        _require_candidates(context)
        scores = {
            candidate.target_id: (
                (candidate.inflight // max(1, candidate.capacity) + 1)
                * max(0.0, candidate.service_time_ms)
                + (0.0 if candidate.ready else max(0.0, candidate.cold_start_ms))
            )
            for candidate in context.candidates
        }
        selected = min(context.candidates, key=lambda item: (scores[item.target_id], item.target_id))
        return SchedulingDecision(
            policy_id=self.policy_id,
            selected_target=selected.target_id,
            scores=scores,
            reason="minimum_expected_finish_ms",
        )


def create_scheduling_policy(policy_id: str) -> SchedulingPolicy:
    policies: dict[str, type[RoundRobinPolicy] | type[LeastLoadedPolicy] | type[EarliestFinishPolicy]] = {
        RoundRobinPolicy.policy_id: RoundRobinPolicy,
        LeastLoadedPolicy.policy_id: LeastLoadedPolicy,
        EarliestFinishPolicy.policy_id: EarliestFinishPolicy,
    }
    try:
        return policies[policy_id]()
    except KeyError as exc:
        raise ValueError(f"unknown_scheduling_policy:{policy_id}") from exc
