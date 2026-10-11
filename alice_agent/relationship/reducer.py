from __future__ import annotations

from dataclasses import replace

from ..domain.event import AgentEvent
from ..domain.perception import PerceptionResult
from .models import RelationshipState


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


class RelationshipReducer:
    def __init__(self, max_delta_per_turn: float = 0.02) -> None:
        self._max_delta = min(0.1, max(0.001, float(max_delta_per_turn)))

    def _bounded(self, current: float, delta: float) -> float:
        delta = min(self._max_delta, max(-self._max_delta, delta))
        return _clamp(current + delta)

    def reduce(
        self,
        old: RelationshipState,
        perception: PerceptionResult,
        event: AgentEvent,
    ) -> RelationshipState:
        signals = perception.social_signals
        affection = signals.get("affection", 0.0)
        vulnerability = signals.get("vulnerability", 0.0)
        reassurance = signals.get("reassurance_seeking", 0.0)
        praise = signals.get("praise", 0.0)
        concern = signals.get("concern", 0.0)
        conflict = signals.get("conflict", 0.0)

        familiarity_delta = min(
            self._max_delta,
            0.003 + 0.012 * (1.0 - old.familiarity),
        )
        trust_delta = (
            0.006 * affection
            + 0.004 * praise
            + 0.004 * concern
            + 0.003 * vulnerability
            - 0.012 * conflict
        )
        closeness_delta = (
            0.008 * affection
            + 0.007 * vulnerability
            + 0.004 * reassurance
            + 0.003 * concern
            - 0.006 * conflict
        )
        first = (
            old.first_interaction_at
            if old.first_interaction_at is not None
            else event.timestamp
        )
        return replace(
            old,
            familiarity=self._bounded(old.familiarity, familiarity_delta),
            trust=self._bounded(old.trust, trust_delta),
            closeness=self._bounded(old.closeness, closeness_delta),
            interaction_count=old.interaction_count + 1,
            first_interaction_at=first,
            last_interaction_at=event.timestamp,
        )
