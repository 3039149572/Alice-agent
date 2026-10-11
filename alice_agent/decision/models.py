from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Any


def clamp01(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("decision values must be finite")
    return min(1.0, max(0.0, number))


class DecisionMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    HYBRID = "hybrid"
    PRIMARY = "primary"


class DecisionType(StrEnum):
    PERCEPTION = "perception"
    MEMORY = "memory"
    PLANNER = "planner"
    PROACTIVE = "proactive"
    MEDIA = "media"
    DAILY_EVENT = "daily_event"
    RESPONSE_TIMING = "response_timing"


@dataclass(frozen=True, slots=True)
class DecisionQuestion:
    type: str
    instructions: str | dict[str, Any] | list[Any]
    criteria: dict[str, Any] | list[Any] | None = None

    def __post_init__(self) -> None:
        if self.type not in {"choice", "score", "noul"}:
            raise ValueError(f"unsupported decision question type: {self.type}")
        if self.type in {"choice", "score"} and not self.criteria:
            raise ValueError(f"{self.type} requires criteria")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if self.criteria is not None:
            payload["criteria"] = self.criteria
        return payload


@dataclass(slots=True)
class DecisionAnswer:
    type: str
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    legend: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.noul is not None:
            self.noul = clamp01(self.noul)
        if self.confidence is not None:
            self.confidence = clamp01(self.confidence)
        self.probabilities = {
            str(key): clamp01(value) for key, value in self.probabilities.items()
        }

    @property
    def effective_confidence(self) -> float:
        if self.confidence is not None:
            return self.confidence
        if self.noul is not None:
            return abs(self.noul - 0.5) * 2.0
        return 0.0


@dataclass(slots=True)
class DecisionResult:
    decision_id: str
    trace_id: str
    decision_type: str
    schema_version: str
    input_hash: str
    model: str
    latency_ms: float
    answers: dict[str, DecisionAnswer] = field(default_factory=dict)
    confidence: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    fallback_used: bool = False
    mode: str = DecisionMode.OFF.value
    error_kind: str | None = None
    cached: bool = False


@dataclass(frozen=True, slots=True)
class ModelRoutingDecision:
    route: str = "normal_llm"
    confidence: float = 0.0
    reason: str = "reserved for a future routing layer"
