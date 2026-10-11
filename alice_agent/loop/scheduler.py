from __future__ import annotations

from ..proactive.models import ScheduledContextEvent


class ContextScheduler:
    """Emits due context only; it never decides whether Alice should act."""

    def __init__(self, store: object) -> None:
        self._store = store

    async def schedule(self, event: ScheduledContextEvent) -> None:
        await self._store.add_scheduled(event)

    async def due(self, now: float) -> list[ScheduledContextEvent]:
        return await self._store.due_scheduled(now)

    async def mark_evaluated(self, event_id: str) -> None:
        await self._store.mark_scheduled(event_id, "evaluated")

    async def maintenance(self, now: float) -> None:
        await self._store.maintenance(now)

    async def unresolved_count(self, session_id: str, now: float) -> int:
        return await self._store.unresolved_count(session_id, now)
