from __future__ import annotations

import asyncio

from ..domain.event import AgentEvent
from ..domain.perception import PerceptionResult
from .models import RelationshipState, SharedExperience
from .reducer import RelationshipReducer


class RelationshipManager:
    def __init__(self, store: object, reducer: RelationshipReducer, enabled: bool = True) -> None:
        self._store = store
        self._reducer = reducer
        self._enabled = enabled
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, user_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def get(self, user_id: str) -> RelationshipState:
        if not self._enabled:
            return RelationshipState(user_id=user_id)
        stored = await self._store.get(user_id)
        return stored or RelationshipState(user_id=user_id)

    async def update(
        self, event: AgentEvent, perception: PerceptionResult
    ) -> RelationshipState:
        if not self._enabled:
            return await self.get(event.user_id)
        lock = await self._lock_for(event.user_id)
        async with lock:
            old = await self.get(event.user_id)
            updated = self._reducer.reduce(old, perception, event)
            await self._store.save(updated)
            return updated

    async def add_shared_experience(self, experience: SharedExperience) -> None:
        if self._enabled:
            await self._store.add_shared_experience(experience)

    async def update_once(self, event: AgentEvent, perception: PerceptionResult, operation_id: str) -> RelationshipState:
        if not self._enabled:
            return await self.get(event.user_id)
        lock = await self._lock_for(event.user_id)
        async with lock:
            return await self._store.apply_once(event.user_id, operation_id, lambda old: self._reducer.reduce(old, perception, event))

    async def close(self) -> None:
        await self._store.close()
