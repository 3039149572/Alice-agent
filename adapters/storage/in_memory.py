from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any


class InMemoryStorageAdapter:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Any] = {}
        self._lock = asyncio.Lock()
        self._operations: dict[str, tuple[str, str]] = {}

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
        self._operations.clear()

    async def mutate_once(self, namespace: str, key: str, operation_id: str, transform):
        async with self._lock:
            done = self._operations.get(operation_id)
            if done is not None and done != (namespace, key):
                raise ValueError("operation id belongs to a different state")
            if done is None:
                result = transform(deepcopy(self._data.get((namespace, key))))
                self._data[(namespace, key)] = deepcopy(result)
                self._operations[operation_id] = (namespace, key)
            return deepcopy(self._data.get((namespace, key)))

    async def delete_prefix(self, namespace: str, prefix: str) -> None:
        async with self._lock:
            for key in list(self._data):
                if key[0] == namespace and key[1].startswith(prefix):
                    del self._data[key]

    async def transform_namespace(self, namespace: str, transform) -> None:
        async with self._lock:
            for key in list(self._data):
                if key[0] != namespace:
                    continue
                updated = transform(deepcopy(self._data[key]))
                if updated is None:
                    del self._data[key]
                else:
                    self._data[key] = deepcopy(updated)
