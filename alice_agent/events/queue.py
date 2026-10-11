from __future__ import annotations

import asyncio

from ..domain.event import AgentEvent


class EventQueue:
    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=maxsize)

    async def put(self, event: AgentEvent) -> None:
        await self._queue.put(event)

    def get_nowait(self) -> AgentEvent:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def task_done(self) -> None:
        self._queue.task_done()
