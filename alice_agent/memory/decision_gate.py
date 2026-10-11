from __future__ import annotations

from ..decision.models import DecisionMode, DecisionType
from ..decision.schemas import MEMORY_SCHEMA, memory_questions
from ..decision.service import DecisionService
from ..domain.event import AgentEvent
from ..domain.memory import MemoryItem
from ..domain.perception import PerceptionResult


class MemoryDecisionGate:
    def __init__(
        self,
        decisions: DecisionService,
        *,
        enabled: bool = True,
        min_relevance: float = 0.12,
    ) -> None:
        self._decisions = decisions
        self._enabled = enabled
        self._min_relevance = min(1.0, max(0.0, min_relevance))

    async def evaluate(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        candidates: list[MemoryItem],
    ) -> list[MemoryItem]:
        if not self._enabled or not self._decisions.enabled or not candidates:
            return candidates
        limited = candidates[:20]
        state = {
            "current_message": (event.text or "")[:2000],
            "current_topic": perception.topic[:200],
            "candidates": [
                {
                    "index": index,
                    "memory_id": item.id,
                    "short_content": item.content[:320],
                    "memory_type": item.memory_type,
                }
                for index, item in enumerate(limited)
            ],
        }
        result = await self._decisions.decide(
            state, memory_questions(len(limited)),
            decision_type=DecisionType.MEMORY.value,
            schema_version=MEMORY_SCHEMA,
            trace_id=event.trace_id,
        )
        if result is None:
            return candidates
        judgments = [
            float(result.answers[f"memory_{index}_relevant"].noul or 0.0)
            for index in range(len(limited))
        ]
        if self._decisions.mode == DecisionMode.SHADOW:
            existing = [
                (item.retrieval_score or 0.0) >= self._min_relevance
                for item in limited
            ]
            jev = [judgment >= 0.5 for judgment in judgments]
            self._decisions.record_comparison(
                DecisionType.MEMORY.value, event.trace_id, jev, existing
            )
            return candidates
        if not self._decisions.policy.may_use_model_decision(
            self._decisions.mode, result.confidence
        ):
            return candidates
        for item, judgment in zip(limited, judgments):
            item.metadata["jev_relevance"] = judgment
            item.metadata["jev_schema_version"] = MEMORY_SCHEMA
        return candidates
