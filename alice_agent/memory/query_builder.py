from __future__ import annotations

from ..domain.event import AgentEvent
from ..domain.memory import MemoryQuery
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState


class MemoryQueryBuilder:
    def build(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        limit: int,
    ) -> MemoryQuery:
        parts = [event.text or ""]
        if perception.topic and perception.topic != "general":
            parts.append(perception.topic)
        if perception.intent and perception.intent != "chat":
            parts.append(perception.intent)
        return MemoryQuery(
            session_id=event.session_id,
            user_id=event.user_id,
            text=" ".join(part for part in parts if part).strip(),
            topic=perception.topic,
            perception=perception,
            state=state,
            limit=max(0, limit),
        )
