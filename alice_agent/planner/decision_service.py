from __future__ import annotations

from dataclasses import asdict

from ..decision.models import DecisionMode, DecisionResult, DecisionType
from ..decision.schemas import PLANNER_SCHEMA, planner_questions
from ..decision.service import DecisionService
from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.memory import MemoryItem
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from ..relationship.models import RelationshipState, relationship_summary


class DecisionAwarePlannerService:
    def __init__(self, existing: object, decisions: DecisionService, *, enabled: bool = True) -> None:
        self._existing = existing
        self._decisions = decisions
        self._enabled = enabled
        self._comparisons: dict[str, dict] = {}

    def pop_comparison(self, trace_id: str) -> dict | None:
        """Return a turn's parallel decisions without adding them to prompts."""
        return self._comparisons.pop(trace_id, None)

    @staticmethod
    def _state(
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        memories: list[MemoryItem],
        history: list[ConversationTurn],
        relationship: RelationshipState,
    ) -> dict:
        return {
            "event": {"text": (event.text or "")[:2000], "type": event.event_type or event.type},
            "perception": perception.prompt_values(),
            "state_summary": {
                "mood_valence": state.mood_valence,
                "mood_arousal": state.mood_arousal,
                "energy": state.energy,
                "activity": state.activity,
                "conversation_mode": state.conversation_mode,
            },
            "relationship_summary": relationship_summary(relationship),
            "memories": [
                {"id": item.id, "content": item.content[:240], "type": item.memory_type}
                for item in memories[:5]
            ],
            "recent_context_summary": [
                {"user": turn.user_text[:180], "assistant": (turn.assistant_text or "")[:180]}
                for turn in history[-3:]
            ],
        }

    @staticmethod
    def _map(result: DecisionResult, available_ids: set[str]) -> ActionPlan:
        answers = result.answers
        action = answers["action"].choice or "reply"
        tone = answers["tone"].choice or "neutral"
        response_length = answers["response_length"].choice or "medium"
        use_memory = float(answers["use_memory"].noul or 0.0) >= 0.65
        memory_ids: list[str] = []
        memory_answer = answers.get("memory_choice")
        if use_memory and memory_answer and memory_answer.choice in available_ids:
            memory_ids = [str(memory_answer.choice)]
        if action == "recall_then_reply" and not memory_ids:
            action = "reply"
        directness = min(1.0, max(0.0, float(answers["directness"].score or 0.0) / 2.0))
        deep = float(answers["requires_deep_reasoning"].noul or 0.0) >= 0.5
        ask_followup = float(answers["ask_followup"].noul or 0.0) >= 0.65
        return ActionPlan(
            action=action,
            intent="structured_decision",
            response_goal="回应当前消息",
            emotion=tone,
            memory_ids=memory_ids,
            source="jev",
            tone=tone,
            response_length=response_length,
            directness=directness,
            ask_followup=ask_followup,
            emotion_style=tone,
            initiative_level=0.25 if ask_followup else 0.0,
            requires_deep_reasoning=deep,
        )

    async def plan(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        memories: list[MemoryItem],
        history: list[ConversationTurn],
        relationship: RelationshipState | None = None,
    ) -> ActionPlan:
        relationship = relationship or RelationshipState(event.user_id)
        if not self._enabled or not self._decisions.enabled:
            return await self._existing.plan(event, perception, state, memories, history, relationship)
        ids = [item.id for item in memories[:5]]
        result = await self._decisions.decide(
            self._state(event, perception, state, memories, history, relationship),
            planner_questions(ids),
            decision_type=DecisionType.PLANNER.value,
            schema_version=PLANNER_SCHEMA,
            trace_id=event.trace_id,
        )
        if result is None:
            self._decisions.record_escalation(DecisionType.PLANNER.value)
            return await self._existing.plan(event, perception, state, memories, history, relationship)
        mapped = self._map(result, set(ids))
        directness_answer = result.answers.get("directness")
        directness_confidence = (
            min(result.confidence, directness_answer.effective_confidence)
            if directness_answer is not None else 0.0
        )
        directness_is_low = (
            directness_confidence < self._decisions.policy.minimum_decision_confidence
        )
        if self._decisions.mode == DecisionMode.SHADOW:
            existing = await self._existing.plan(event, perception, state, memories, history, relationship)
            self._comparisons[event.trace_id] = {
                "jev": asdict(mapped), "llm": asdict(existing),
                "jev_raw": asdict(result), "selected_decision_source": "llm",
            }
            self._decisions.record_comparison(
                DecisionType.PLANNER.value,
                event.trace_id,
                mapped.action,
                existing.action,
            )
            return existing
        if not mapped.requires_deep_reasoning and self._decisions.policy.may_use_model_decision(
            self._decisions.mode, result.confidence
        ):
            if directness_is_low:
                existing = await self._existing.plan(
                    event, perception, state, memories, history, relationship
                )
                llm_plan = asdict(existing)
                jev_plan = asdict(mapped)
                weight = 0.5 * directness_confidence / self._decisions.policy.minimum_decision_confidence
                mapped.directness = (
                    (1.0 - weight) * existing.directness + weight * mapped.directness
                )
                self._comparisons[event.trace_id] = {
                    "jev": jev_plan, "llm": llm_plan,
                    "jev_raw": asdict(result), "selected_decision_source": "jev",
                    "numeric_decision_source": "jev_llm_blend",
                }
                self._decisions.record_escalation(DecisionType.PLANNER.value)
                return mapped
            self._decisions.record_llm_avoided(DecisionType.PLANNER.value)
            return mapped
        self._decisions.record_escalation(DecisionType.PLANNER.value)
        existing = await self._existing.plan(event, perception, state, memories, history, relationship)
        numeric_source = "llm"
        if directness_is_low:
            if directness_answer is not None and directness_answer.score is not None:
                weight = 0.5 * directness_confidence / self._decisions.policy.minimum_decision_confidence
                existing.directness = (
                    (1.0 - weight) * existing.directness
                    + weight * mapped.directness
                )
                numeric_source = "jev_llm_blend"
        self._comparisons[event.trace_id] = {
            "jev": asdict(mapped), "llm": asdict(existing),
            "jev_raw": asdict(result), "selected_decision_source": "llm",
            "numeric_decision_source": numeric_source,
        }
        return existing
