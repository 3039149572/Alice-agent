from __future__ import annotations

import asyncio
from collections import OrderedDict

from ..domain.event import AgentEvent
from .queue import EventQueue


class EventBus:
    """Small in-process bus with bounded recent-ID deduplication."""

    def __init__(self, recent_id_limit: int = 4096) -> None:
        self._queue = EventQueue()
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._recent_id_limit = max(128, recent_id_limit)
        self._lock = asyncio.Lock()

    async def publish(self, event: AgentEvent) -> bool:
        async with self._lock:
            if event.event_id in self._recent:
                return False
            self._recent[event.event_id] = None
            while len(self._recent) > self._recent_id_limit:
                self._recent.popitem(last=False)
        await self._queue.put(event)
        return True

    def drain_nowait(self, limit: int = 1000) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        for _ in range(max(0, limit)):
            if self._queue.empty():
                break
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            events.append(event)
            self._queue.task_done()
        return events
