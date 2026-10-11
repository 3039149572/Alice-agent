from __future__ import annotations

import math
import re
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

    def restore(self, payload: object) -> None:
        """Merge validated counters only; never restore text or derived ratios."""
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
        ):
            return
        buckets = payload.get("buckets")
        if not isinstance(buckets, dict) or len(buckets) > 128:
            return
        for name, values in buckets.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", name):
                continue
            if not isinstance(values, dict):
                continue
            validated = {}
            for field_name, default in asdict(DecisionMetricBucket()).items():
                value = values.get(field_name, default)
                if field_name == "latency_total_ms":
                    if type(value) not in (float, int) or not 0 <= value <= 1e18 or not math.isfinite(value):
                        break
                elif type(value) is not int or not 0 <= value <= 10**18:
                    break
                validated[field_name] = value
            else:
                bucket = self.bucket(name)
                for field_name, value in validated.items():
                    setattr(bucket, field_name, getattr(bucket, field_name) + value)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for name, bucket in self._buckets.items():
            values = asdict(bucket)
            values["latency_avg_ms"] = bucket.latency_avg_ms
            values["fallback_rate"] = bucket.fallback_rate
            values["disagreement_rate"] = bucket.disagreement_rate
            result[name] = values
        return result
