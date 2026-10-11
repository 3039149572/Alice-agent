from __future__ import annotations

from typing import Protocol

from ..domain.memory import MemoryItem, MemoryObservation, MemoryQuery


class MemoryPort(Protocol):
    async def recall(self, query: MemoryQuery) -> list[MemoryItem]: ...

    async def observe(self, observation: MemoryObservation) -> bool: ...

    async def update(self, item: MemoryItem) -> None: ...

    async def get(self, memory_id: str) -> MemoryItem | None: ...

    async def close(self) -> None: ...
