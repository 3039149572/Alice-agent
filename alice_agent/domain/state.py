from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AgentState:
    mood_valence: float = 0.0
    mood_arousal: float = 0.3
    mood_dominance: float = 0.5
    energy: float = 0.7
    stress: float = 0.2
    loneliness: float = 0.15
    activity: str = "idle"
    current_topic: str | None = None
    conversation_mode: str = "casual"
    daily_event_day: str | None = None
    daily_events: list[str] = field(default_factory=list)
    topic_hooks: list[str] = field(default_factory=list)
