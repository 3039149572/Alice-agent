from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..decision.models import DecisionMode, DecisionType
from ..decision.schemas import PROACTIVE_SCHEMA, proactive_questions
from ..decision.service import DecisionService
from ..domain.memory import MemoryItem
from ..drives.models import DriveState
from ..relationship.models import RelationshipState, relationship_summary
from .models import CandidateAction, UserAvailability


@dataclass(frozen=True, slots=True)
class ProactiveGateDecision:
    action: str
    reason: str
    shadow_action: str | None = None
    retryable: bool = False
    diagnostic: dict = field(default_factory=dict)


class ProactiveDecisionGate:
    def __init__(self, decisions: DecisionService, *, enabled: bool = True) -> None:
        self._decisions = decisions
        self._enabled = enabled

    def record_shadow_comparison(
        self, trace_id: str, shadow_action: str, existing_action: str
    ) -> None:
        self._decisions.record_comparison(
            DecisionType.PROACTIVE.value, trace_id, shadow_action, existing_action
        )

    async def evaluate(
        self,
        candidate: CandidateAction,
        drives: DriveState,
        relationship: RelationshipState,
        memories: list[MemoryItem],
        availability: UserAvailability,
        trace_id: str,
    ) -> ProactiveGateDecision:
        attempt = {"mode": self._decisions.mode.value}
        def make(action, reason, **kwargs):
            return ProactiveGateDecision(action, reason, diagnostic=dict(attempt), **kwargs)
        if not self._enabled or not self._decisions.enabled:
            attempt["status"] = "disabled"
            return make("existing", "Jev proactive gate disabled")
        state = {
            "candidate": {
                "type": candidate.action_type.value,
                "reason": candidate.reason[:300],
                "topic": candidate.topic,
                "urgency": candidate.urgency,
                "relevance": candidate.relevance,
                "interruption_cost": candidate.interruption_cost,
                "context_summary": str(candidate.metadata.get("summary", ""))[:320],
            },
            "drives": asdict(drives),
            "relationship": relationship_summary(relationship),
            "memories": [
                {"id": item.id, "content": item.content[:240], "type": item.memory_type}
                for item in memories[:3]
            ],
            "availability": asdict(availability),
            "deterministic_policy": {"cooldown_passed": True, "daily_budget_passed": True},
        }
        result = await self._decisions.decide(
            state, proactive_questions(),
            decision_type=DecisionType.PROACTIVE.value,
            schema_version=PROACTIVE_SCHEMA,
            trace_id=trace_id,
            attempt_diagnostics=attempt,
        )
        if result is None:
            if self._decisions.mode == DecisionMode.SHADOW:
                return make("existing", "shadow failure does not affect behavior")
            return make("do_nothing", "Jev proactive gate unavailable", retryable=True)
        answers = result.answers
        choice = answers["action"].choice or "do_nothing"
        if self._decisions.mode == DecisionMode.SHADOW:
            return make("existing", "shadow mode", shadow_action=choice)
        # Proactive decisions always require the high threshold, even in primary mode.
        if (
            result.confidence < self._decisions.policy.minimum_decision_confidence
            or self._decisions.policy.classify(result.confidence) != "high"
        ):
            return make("do_nothing", "Jev proactive confidence below auto threshold")
        send_probability = float(answers["action"].probabilities.get("send", 0.0))
        natural = float(answers["natural_reason_exists"].noul or 0.0)
        interruption = float(answers["likely_to_interrupt"].noul or 0.0)
        relevant = float(answers["relevant_context_exists"].noul or 0.0)
        appropriateness = min(1.0, max(0.0, float(answers["appropriateness"].score or 0.0) / 4.0))
        deep = float(answers["requires_deep_reasoning"].noul or 0.0)
        threshold = self._decisions.policy.auto_threshold
        if (
            choice == "send"
            and send_probability >= threshold
            and natural >= threshold
            and interruption <= 1.0 - threshold
            and relevant >= self._decisions.policy.review_threshold
            and appropriateness >= 0.75
            and deep < 0.5
        ):
            self._decisions.record_llm_avoided(DecisionType.PROACTIVE.value)
            return make("send", "Jev high-confidence proactive approval")
        if choice == "defer":
            return make("defer", "Jev chose defer")
        return make("do_nothing", "Jev safety conditions rejected send")
