from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass


@dataclass(slots=True)
class DecisionMetricBucket:
    calls: int = 0
    latency_total_ms: float = 0.0
    errors: int = 0
    timeouts: int = 0
    fallbacks: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    llm_escalations: int = 0
    existing_comparisons: int = 0
    existing_disagreements: int = 0
    llm_calls_avoided: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def latency_avg_ms(self) -> float:
        return self.latency_total_ms / self.calls if self.calls else 0.0

    @property
    def fallback_rate(self) -> float:
        return self.fallbacks / self.calls if self.calls else 0.0

    @property
    def disagreement_rate(self) -> float:
        return (
            self.existing_disagreements / self.existing_comparisons
            if self.existing_comparisons else 0.0
        )


class DecisionMetrics:
    def __init__(self) -> None:
        self._buckets: defaultdict[str, DecisionMetricBucket] = defaultdict(DecisionMetricBucket)

    def bucket(self, decision_type: str) -> DecisionMetricBucket:
        return self._buckets[decision_type]

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for name, bucket in self._buckets.items():
            values = asdict(bucket)
            values["latency_avg_ms"] = bucket.latency_avg_ms
            values["fallback_rate"] = bucket.fallback_rate
            values["disagreement_rate"] = bucket.disagreement_rate
            result[name] = values
        return result
