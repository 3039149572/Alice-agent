from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any


class InMemoryStorageAdapter:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Any] = {}
        self._lock = asyncio.Lock()

    async def get(self, namespace: str, key: str) -> Any | None:
        async with self._lock:
            value = self._data.get((namespace, key))
            return deepcopy(value)

    async def set(self, namespace: str, key: str, value: Any) -> None:
        async with self._lock:
            self._data[(namespace, key)] = deepcopy(value)

    async def delete(self, namespace: str, key: str) -> None:
        async with self._lock:
            self._data.pop((namespace, key), None)

    async def close(self) -> None:
        self._data.clear()
