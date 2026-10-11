from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .models import DecisionMode


@dataclass(frozen=True, slots=True)
class DecisionEscalationPolicy:
    auto_threshold: float = 0.85
    review_threshold: float = 0.80
    minimum_decision_confidence: ClassVar[float] = 0.80

    def classify(self, confidence: float) -> str:
        value = min(1.0, max(0.0, float(confidence)))
        if value < self.minimum_decision_confidence:
            return "low"
        if value >= self.auto_threshold:
            return "high"
        if value >= self.review_threshold:
            return "medium"
        return "low"

    def may_use_model_decision(self, mode: DecisionMode, confidence: float) -> bool:
        if mode in {DecisionMode.OFF, DecisionMode.SHADOW}:
            return False
        if confidence < self.minimum_decision_confidence:
            return False
        level = self.classify(confidence)
        if mode == DecisionMode.HYBRID:
            return level == "high"
        return level in {"high", "medium"}
