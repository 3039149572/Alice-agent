from __future__ import annotations

from dataclasses import dataclass, field


VALID_ACTIONS = frozenset(
    {"reply", "ignore", "acknowledge", "comfort", "clarify", "tease", "recall_then_reply", "wait"}
)


@dataclass(slots=True)
class ActionPlan:
    action: str = "reply"
    intent: str = "respond"
    response_goal: str = "回应当前消息"
    emotion: str = "calm"
    energy: float = 0.5
    memory_ids: list[str] = field(default_factory=list)
    expression: str | None = None
    voice_style: str | None = None
    source: str = "reactive"
    tone: str = "neutral"
    response_length: str = "medium"
    directness: float = 0.5
    ask_followup: bool = False
    emotion_style: str = "calm"
    initiative_level: float = 0.0
    requires_deep_reasoning: bool = False

    @classmethod
    def fallback(cls) -> "ActionPlan":
        return cls()
