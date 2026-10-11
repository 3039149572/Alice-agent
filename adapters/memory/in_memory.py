from __future__ import annotations

import asyncio
import re
from copy import deepcopy

from ...alice_agent.domain.memory import MemoryItem, MemoryObservation, MemoryQuery


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    latin = set(re.findall(r"[a-z0-9_]+", lowered))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    grams = {cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))}
    if len(cjk) == 1:
        grams.add(cjk)
    return latin | grams


class InMemoryMemoryAdapter:
    """Development-only volatile memory, isolated by session metadata."""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._lock = asyncio.Lock()

    async def recall(self, query: MemoryQuery) -> list[MemoryItem]:
        query_tokens = _tokens(query.text)
        topic_tokens = _tokens(query.topic)
        scored: list[tuple[float, MemoryItem]] = []
        async with self._lock:
            for item in self._items.values():
                if item.metadata.get("session_id") != query.session_id:
                    continue
                item_tokens = _tokens(item.content)
                lexical = len(query_tokens & item_tokens) / max(1, len(query_tokens | item_tokens))
                item_topic_tokens = _tokens(str(item.metadata.get("topic", "")))
                topic_score = len(topic_tokens & item_topic_tokens) / max(1, len(topic_tokens | item_topic_tokens))
                if lexical <= 0.0 and topic_score <= 0.0:
                    continue
                relevance = 0.8 * lexical + 0.2 * topic_score
                item.retrieval_score = relevance
                scored.append((relevance, item))
            scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
            selected = [deepcopy(item) for _, item in scored[: query.limit]]
            return selected

    async def observe(self, observation: MemoryObservation) -> bool:
        if not observation.persist_candidate:
            return False
        async with self._lock:
            self._items[observation.item.id] = deepcopy(observation.item)
        return True

    async def update(self, item: MemoryItem) -> None:
        async with self._lock:
            if item.id not in self._items:
                raise KeyError(item.id)
            self._items[item.id] = deepcopy(item)

    async def get(self, memory_id: str) -> MemoryItem | None:
        async with self._lock:
            item = self._items.get(memory_id)
            return deepcopy(item) if item else None

    async def close(self) -> None:
        async with self._lock:
            self._items.clear()

    async def count(self) -> int:
        async with self._lock:
            return len(self._items)
