from __future__ import annotations

from dataclasses import dataclass, field


SOCIAL_SIGNAL_KEYS = (
    "affection",
    "complaint",
    "teasing",
    "conflict",
    "reassurance_seeking",
    "vulnerability",
    "praise",
    "concern",
)


def empty_social_signals() -> dict[str, float]:
    return {key: 0.0 for key in SOCIAL_SIGNAL_KEYS}


@dataclass(slots=True)
class PerceptionResult:
    intent: str = "chat"
    topic: str = "general"
    user_emotion: str = "neutral"
    valence: float = 0.0
    arousal: float = 0.2
    dominance: float = 0.5
    social_signals: dict[str, float] = field(default_factory=empty_social_signals)
    importance: float = 0.2
    memory_candidate: bool = False
    confidence: float = 0.0
    # None is used by legacy adapters that provide already-validated values.
    # The JSON parser supplies explicit field names so absent LLM numbers are
    # never mistaken for predictions during Jev/LLM blending.
    numeric_evidence: frozenset[str] | None = None
    # Diagnostics are retained for test logging only. ContextBuilder projects
    # an explicit prompt-safe subset instead of serializing this field.
    emotion_comparison: dict[str, object] | None = None

    def prompt_values(self) -> dict[str, object]:
        """Fields safe and relevant for the assistant to use as perception."""
        return {
            "intent": self.intent,
            "topic": self.topic,
            "user_emotion": self.user_emotion,
            "valence": self.valence,
            "arousal": self.arousal,
            "dominance": self.dominance,
            "social_signals": dict(self.social_signals),
            "importance": self.importance,
            "memory_candidate": self.memory_candidate,
            "confidence": self.confidence,
        }

    @classmethod
    def fallback(cls) -> "PerceptionResult":
        return cls()
