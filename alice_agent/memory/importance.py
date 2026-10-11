from __future__ import annotations

from dataclasses import dataclass

from ..domain.memory import MemoryItem
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from ..relationship.models import RelationshipState


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


@dataclass(frozen=True, slots=True)
class MemoryImportance:
    importance: float
    emotional_weight: float
    relationship_weight: float


class MemoryImportanceEvaluator:
    """Deterministic v0.4 evaluator; it performs no extra LLM call."""

    _SOURCE_PRIOR = {"L0": 0.10, "L1": 0.18, "L2": 0.24, "L3": 0.28, "LOCAL": 0.12}

    def evaluate(
        self,
        perception: PerceptionResult,
        state: AgentState,
        item: MemoryItem,
        relationship: RelationshipState,
        *,
        new_observation: bool = False,
    ) -> MemoryImportance:
        signals = perception.social_signals
        affection = signals.get("affection", 0.0)
        vulnerability = signals.get("vulnerability", 0.0)
        conflict = signals.get("conflict", 0.0)
        reassurance = signals.get("reassurance_seeking", 0.0)
        praise = signals.get("praise", 0.0)
        concern = signals.get("concern", 0.0)
        social_peak = max(affection, vulnerability, conflict, reassurance, praise, concern)
        source_prior = self._SOURCE_PRIOR.get(item.source_layer, 0.12)

        if new_observation:
            importance = (
                0.62 * perception.importance
                + 0.13 * perception.confidence
                + 0.10 * perception.arousal
                + 0.10 * social_peak
                + 0.05 * source_prior
            )
            emotional = (
                0.28 * abs(perception.valence)
                + 0.22 * perception.arousal
                + 0.03 * state.mood_arousal
                + 0.18 * vulnerability
                + 0.14 * conflict
                + 0.10 * affection
                + 0.08 * max(praise, concern)
            )
            relationship_weight = (
                0.30 * affection
                + 0.24 * vulnerability
                + 0.16 * reassurance
                + 0.14 * conflict
                + 0.09 * praise
                + 0.07 * concern
            ) * (0.9 + 0.1 * relationship.closeness)
        else:
            importance = max(item.importance, source_prior * item.confidence)
            emotional = item.emotional_weight
            relationship_weight = item.relationship_weight
            if relationship_weight > 0.05:
                relationship_weight += 0.03 * relationship.closeness

        return MemoryImportance(
            importance=_clamp(importance),
            emotional_weight=_clamp(emotional),
            relationship_weight=_clamp(relationship_weight),
        )
