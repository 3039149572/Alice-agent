from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


@dataclass(slots=True)
class RelationshipState:
    user_id: str
    familiarity: float = 0.0
    trust: float = 0.5
    closeness: float = 0.0
    interaction_count: int = 0
    first_interaction_at: float | None = None
    last_interaction_at: float | None = None

    def __post_init__(self) -> None:
        self.familiarity = _clamp(self.familiarity)
        self.trust = _clamp(self.trust)
        self.closeness = _clamp(self.closeness)
        self.interaction_count = max(0, int(self.interaction_count))


@dataclass(slots=True)
class SharedExperience:
    id: str
    user_id: str
    title: str
    summary: str
    timestamp: float
    importance: float
    emotional_weight: float
    relationship_weight: float
    related_memory_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.importance = _clamp(self.importance)
        self.emotional_weight = _clamp(self.emotional_weight)
        self.relationship_weight = _clamp(self.relationship_weight)


def relationship_summary(state: RelationshipState) -> list[str]:
    if state.interaction_count == 0:
        return ["刚开始认识", "关系仍在建立中"]
    familiarity = (
        "已经很熟悉" if state.familiarity >= 0.7 else
        "已经比较熟悉" if state.familiarity >= 0.35 else
        "仍在逐渐熟悉"
    )
    trust = (
        "相互信任程度较高" if state.trust >= 0.7 else
        "信任关系稳定" if state.trust >= 0.4 else
        "信任需要谨慎维护"
    )
    closeness = (
        "关系较为亲近" if state.closeness >= 0.65 else
        "关系正在自然靠近" if state.closeness >= 0.25 else
        "保持友好但不过度亲近"
    )
    return [familiarity, trust, closeness]
