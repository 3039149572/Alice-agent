from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from ..domain.memory import MemoryItem, MemoryQuery, MemoryScore
from .policies import MemoryDecayPolicy, NoDecayPolicy


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    latin = set(re.findall(r"[a-z0-9_]+", lowered))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    grams = {cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))}
    if len(cjk) == 1:
        grams.add(cjk)
    return latin | grams


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class MemoryRerankConfig:
    min_relevance: float = 0.12
    semantic_weight: float = 0.65
    importance_weight: float = 0.15
    emotion_weight: float = 0.07
    relationship_weight: float = 0.08
    recency_weight: float = 0.05
    jev_relevance_blend: float = 0.25

    def __post_init__(self) -> None:
        for name in (
            "min_relevance", "semantic_weight", "importance_weight",
            "emotion_weight", "relationship_weight", "recency_weight",
            "jev_relevance_blend",
        ):
            object.__setattr__(
                self, name, min(1.0, max(0.0, float(getattr(self, name))))
            )


@dataclass(slots=True)
class RerankResult:
    selected: list[MemoryItem]
    scores: list[MemoryScore]


class MemoryReranker:
    def __init__(
        self,
        config: MemoryRerankConfig | None = None,
        decay_policy: MemoryDecayPolicy | None = None,
    ) -> None:
        self._config = config or MemoryRerankConfig()
        self._decay = decay_policy or NoDecayPolicy()

    @staticmethod
    def _contextual_relevance(query: MemoryQuery, item: MemoryItem) -> float:
        if item.retrieval_score is not None:
            return item.retrieval_score
        query_tokens = _tokens(f"{query.text} {query.topic}")
        item_tokens = _tokens(f"{item.content} {item.metadata.get('topic', '')}")
        return _jaccard(query_tokens, item_tokens)

    @staticmethod
    def _recency(item: MemoryItem, now: float) -> float:
        if item.created_at is None:
            return 0.25
        age_days = max(0.0, now - item.created_at) / 86400.0
        return math.exp(-age_days / 180.0)

    def rerank(
        self,
        items: list[MemoryItem],
        limit: int,
        query: MemoryQuery | None = None,
        now: float | None = None,
    ) -> RerankResult:
        if query is None:
            seen: set[str] = set()
            selected = []
            for item in items:
                if item.id not in seen:
                    seen.add(item.id)
                    selected.append(item)
            return RerankResult(selected[: max(0, limit)], [])

        current_time = now if now is not None else time.time()
        seen: set[str] = set()
        accepted: list[tuple[float, MemoryItem]] = []
        scores: list[MemoryScore] = []
        for item in items:
            if item.id in seen:
                continue
            seen.add(item.id)
            relevance = self._contextual_relevance(query, item)
            recency = self._recency(item, current_time)
            if relevance < self._config.min_relevance:
                scores.append(MemoryScore(
                    item.id, relevance, item.importance, item.emotional_weight,
                    item.relationship_weight, recency, 0.0, False,
                    "relevance below threshold",
                ))
                continue
            jev_relevance = item.metadata.get("jev_relevance")
            effective_relevance = relevance
            if isinstance(jev_relevance, (int, float)):
                effective_relevance = (
                    relevance * (1.0 - self._config.jev_relevance_blend)
                    + min(1.0, max(0.0, float(jev_relevance)))
                    * self._config.jev_relevance_blend
                )
            final = (
                effective_relevance * self._config.semantic_weight
                + item.importance * self._config.importance_weight
                + item.emotional_weight * self._config.emotion_weight
                + item.relationship_weight * self._config.relationship_weight
                + recency * self._config.recency_weight
            ) * self._decay.calculate(item, current_time)
            final = min(1.0, max(0.0, final))
            item.metadata["alice_ranking"] = {
                "retrieval": relevance,
                "jev_relevance": jev_relevance,
                "effective_relevance": effective_relevance,
                "recency": recency,
                "final_score": final,
            }
            scores.append(MemoryScore(
                item.id, relevance, item.importance, item.emotional_weight,
                item.relationship_weight, recency, final, True,
            ))
            accepted.append((final, item))
        accepted.sort(key=lambda pair: pair[0], reverse=True)
        return RerankResult(
            selected=[item for _, item in accepted[: max(0, limit)]],
            scores=scores,
        )
