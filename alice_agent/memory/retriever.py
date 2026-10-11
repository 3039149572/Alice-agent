from __future__ import annotations

from ..domain.event import AgentEvent
from ..domain.memory import MemoryItem, MemoryQuery
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from ..ports.memory import MemoryPort
from .query_builder import MemoryQueryBuilder


class MemoryRetriever:
    def __init__(
        self, port: MemoryPort, recall_limit: int,
        query_builder: MemoryQueryBuilder | None = None,
    ) -> None:
        self._port = port
        self._recall_limit = max(0, recall_limit)
        self._query_builder = query_builder or MemoryQueryBuilder()

    async def recall(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
    ) -> tuple[MemoryQuery, list[MemoryItem]]:
        query = self._query_builder.build(
            event, perception, state, max(10, self._recall_limit * 2)
        )
        if self._recall_limit <= 0:
            return query, []
        return query, await self._port.recall(query)
