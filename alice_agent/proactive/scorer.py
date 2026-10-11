from __future__ import annotations

from dataclasses import dataclass

from ..drives.models import DriveState
from ..relationship.models import RelationshipState
from .models import ActionDecision, CandidateAction, CandidateActionType


@dataclass(frozen=True, slots=True)
class ActionScoreConfig:
    threshold: float = 0.72
    defer_threshold: float = 0.60
    social_weight: float = 0.18
    curiosity_weight: float = 0.24
    relationship_weight: float = 0.15
    relevance_weight: float = 0.55
    urgency_weight: float = 0.20
    interruption_weight: float = 0.15
    rest_weight: float = 0.08


class ActionScorer:
    def __init__(self, config: ActionScoreConfig | None = None) -> None:
        self._config = config or ActionScoreConfig()

    def score(
        self,
        candidate: CandidateAction,
        drives: DriveState,
        relationship: RelationshipState,
        *,
        threshold_override: float | None = None,
        defer_threshold_override: float | None = None,
    ) -> ActionDecision:
        if candidate.action_type == CandidateActionType.DO_NOTHING:
            return ActionDecision("do_nothing", 0.0, candidate.reason)
        relationship_factor = (
            relationship.familiarity + relationship.trust + relationship.closeness
        ) / 3.0
        c = self._config
        score = (
            drives.social_need * c.social_weight
            + drives.curiosity * c.curiosity_weight
            + relationship_factor * c.relationship_weight
            + candidate.relevance * c.relevance_weight
            + candidate.urgency * c.urgency_weight
            - candidate.interruption_cost * c.interruption_weight
            - drives.rest_need * c.rest_weight
        )
        score = min(1.0, max(0.0, score))
        threshold = c.threshold if threshold_override is None else min(1.0, max(0.0, float(threshold_override)))
        defer_threshold = (
            c.defer_threshold if defer_threshold_override is None
            else min(threshold, max(0.0, float(defer_threshold_override)))
        )
        if score >= threshold:
            return ActionDecision("allow", score, "score reached proactive threshold")
        if score >= defer_threshold:
            return ActionDecision("defer", score, "score is plausible but not strong enough")
        return ActionDecision("do_nothing", score, "score below conservative threshold")
