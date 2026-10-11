from __future__ import annotations

import asyncio

from ..domain.state import AgentState
from ..relationship.models import RelationshipState
from .models import DriveState
from .reducer import DriveReducer


class DriveManager:
    def __init__(self, store: object, reducer: DriveReducer) -> None:
        self._store = store
        self._reducer = reducer
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    async def get(self, session_id: str, user_id: str) -> DriveState:
        stored = await self._store.get_drive(session_id)
        return stored or DriveState(session_id=session_id, user_id=user_id)

    async def advance(
        self,
        session_id: str,
        user_id: str,
        now: float,
        state: AgentState,
        relationship: RelationshipState,
        unresolved_context_count: int,
    ) -> DriveState:
        lock = await self._lock_for(session_id)
        async with lock:
            old = await self.get(session_id, user_id)
            elapsed = 0.0 if old.updated_at is None else max(0.0, now - old.updated_at)
            updated = self._reducer.advance(
                old, elapsed, state, relationship,
                unresolved_context_count=unresolved_context_count, now=now,
            )
            await self._store.save_drive(updated)
            return updated

    async def on_user_message(
        self, session_id: str, user_id: str, timestamp: float
    ) -> DriveState:
        lock = await self._lock_for(session_id)
        async with lock:
            old = await self.get(session_id, user_id)
            updated = self._reducer.on_user_message(old, timestamp)
            await self._store.save_drive(updated)
            return updated
