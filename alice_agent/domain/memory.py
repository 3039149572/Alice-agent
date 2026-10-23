from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .perception import PerceptionResult
from .state import AgentState


@dataclass(slots=True)
class MemoryItem:
    id: str
    content: str
    memory_type: str = "conversation_event"
    source_layer: str = "LOCAL"
    retrieval_score: float | None = None
    importance: float = 0.0
    emotional_weight: float = 0.0
    relationship_weight: float = 0.0
    confidence: float = 0.0
    access_count: int = 0
    last_accessed_at: float | None = None
    created_at: float | None = None
    pinned: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_layer = self.source_layer.upper()
        if self.source_layer not in {"L0", "L1", "L2", "L3", "LOCAL"}:
            self.source_layer = "LOCAL"
        for name in (
            "importance",
            "emotional_weight",
            "relationship_weight",
            "confidence",
        ):
            setattr(self, name, min(1.0, max(0.0, float(getattr(self, name)))))
        if self.retrieval_score is not None:
            self.retrieval_score = min(1.0, max(0.0, float(self.retrieval_score)))
        self.access_count = max(0, int(self.access_count))


@dataclass(slots=True)
class MemoryMetadata:
    memory_id: str
    importance: float = 0.0
    emotional_weight: float = 0.0
    relationship_weight: float = 0.0
    confidence: float = 0.0
    access_count: int = 0
    last_accessed_at: float | None = None
    pinned: bool = False
    decay_rate: float = 0.0
    created_at: float | None = None
    updated_at: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "importance",
            "emotional_weight",
            "relationship_weight",
            "confidence",
            "decay_rate",
        ):
            setattr(self, name, min(1.0, max(0.0, float(getattr(self, name)))))
        self.access_count = max(0, int(self.access_count))


@dataclass(slots=True)
class MemoryObservation:
    """A completed turn offered to a memory backend.

    Backends decide whether this becomes a long-term item, a local turn-journal
    entry, or no write. This keeps storage policy outside the core.
    """

    item: MemoryItem
    assistant_text: str | None
    persist_candidate: bool
    persisted_items: list[MemoryItem] = field(default_factory=list)


@dataclass(slots=True)
class MemoryQuery:
    session_id: str
    user_id: str
    text: str
    topic: str
    perception: PerceptionResult
    state: AgentState
    limit: int = 5


@dataclass(frozen=True, slots=True)
class MemoryScore:
    memory_id: str
    retrieval: float
    importance: float
    emotion: float
    relationship: float
    recency: float
    final_score: float
    accepted: bool
    reason: str | None = None
