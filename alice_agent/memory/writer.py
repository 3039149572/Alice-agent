from __future__ import annotations

import uuid

from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.memory import MemoryItem, MemoryObservation
from ..domain.perception import PerceptionResult
from ..domain.response import AgentResponse
from ..domain.state import AgentState
from ..ports.memory import MemoryPort
from ..relationship.models import RelationshipState
from .importance import MemoryImportanceEvaluator


class MemoryWriter:
    def __init__(
        self, port: MemoryPort, importance_threshold: float,
        evaluator: MemoryImportanceEvaluator | None = None,
    ) -> None:
        self._port = port
        self._threshold = min(1.0, max(0.0, importance_threshold))
        self._evaluator = evaluator or MemoryImportanceEvaluator()

    def build_observation(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        plan: ActionPlan,
        response: AgentResponse,
        relationship: RelationshipState,
    ) -> MemoryObservation | None:
        if not event.text:
            return None
        item = MemoryItem(
            id=f"mem_{uuid.uuid4().hex}",
            content=event.text,
            memory_type="conversation_event",
            source_layer="LOCAL",
            confidence=perception.confidence,
            created_at=event.timestamp,
            metadata={
                "session_id": event.session_id,
                "user_id": event.user_id,
                "source": event.source,
                "trace_id": event.trace_id,
                "topic": perception.topic,
                "intent": perception.intent,
                "action": plan.action,
                "response_generated": bool(response.text),
            },
        )
        evaluated = self._evaluator.evaluate(
            perception, state, item, relationship, new_observation=True
        )
        item.importance = evaluated.importance
        item.emotional_weight = evaluated.emotional_weight
        item.relationship_weight = evaluated.relationship_weight
        return MemoryObservation(
            item=item,
            assistant_text=response.text,
            persist_candidate=(
                perception.memory_candidate
                and item.importance >= self._threshold
            ),
        )
