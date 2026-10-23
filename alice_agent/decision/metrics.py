from __future__ import annotations

import math
import re
import copy
from datetime import datetime, timezone, date
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
        self._groups: dict[tuple[str, str, str, str], dict] = {}

    _LATENCY_BOUNDS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)

    def record_group(self, task: str, model: str, mode: str, latency_ms: float, status: str, usage: dict):
        day = datetime.now(timezone.utc).date().isoformat()
        key = (day, str(model)[:120], mode, task)
        if key not in self._groups:
            if len(self._groups) >= 256:
                self._groups.pop(min(self._groups))
            self._groups[key] = {
                "date_utc": day, "model": key[1], "mode": mode, "decision_type": task,
                "calls": 0, "errors": 0, "timeouts": 0, "cancelled": 0,
                "input_tokens": 0, "output_tokens": 0, "latency_total_ms": 0.0,
                "latency_max_ms": 0.0, "latency_bins": [0] * 13,
            }
        row = self._groups[key]
        row["calls"] += 1
        if status in {"error", "timeout", "cancelled"}:
            row[{"error": "errors", "timeout": "timeouts", "cancelled": "cancelled"}[status]] += 1
        for name in ("input_tokens", "output_tokens"):
            value = usage.get(name, 0)
            if type(value) is int and 0 <= value <= 10**12:
                row[name] += value
        latency_ms = min(1e9, max(0.0, float(latency_ms)))
        row["latency_total_ms"] += latency_ms
        row["latency_max_ms"] = max(row["latency_max_ms"], latency_ms)
        slot = next((index for index, bound in enumerate(self._LATENCY_BOUNDS) if latency_ms <= bound), 12)
        row["latency_bins"][slot] += 1

    def grouped_snapshot(self, *, derived: bool = True) -> list[dict]:
        result = copy.deepcopy([self._groups[key] for key in sorted(self._groups)])
        if derived:
            for row in result:
                row["latency_avg_ms"] = row["latency_total_ms"] / row["calls"] if row["calls"] else 0
                rank, count = max(1, math.ceil(row["calls"] * 0.95)), 0
                for index, value in enumerate(row["latency_bins"]):
                    count += value
                    if count >= rank:
                        row["latency_p95_upper_ms"] = self._LATENCY_BOUNDS[index] if index < 12 else row["latency_max_ms"]
                        break
        return result

    def restore_groups(self, groups: object) -> None:
        if not isinstance(groups, list) or len(groups) > 256:
            return
        allowed = {"perception", "memory", "planner", "proactive", "media", "daily_event", "response_timing"}
        for row in groups:
            if not isinstance(row, dict) or row.get("decision_type") not in allowed or row.get("mode") not in {"off", "shadow", "hybrid", "primary"}:
                continue
            try:
                day = date.fromisoformat(row.get("date_utc", "")).isoformat()
            except (TypeError, ValueError):
                continue
            model = row.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,120}", model):
                continue
            counters = ("calls", "errors", "timeouts", "cancelled", "input_tokens", "output_tokens")
            if any(type(row.get(name)) is not int or not 0 <= row[name] <= 10**18 for name in counters):
                continue
            if row["errors"] + row["timeouts"] + row["cancelled"] > row["calls"]:
                continue
            if any(type(row.get(name)) not in (int, float) or not math.isfinite(row[name]) or not 0 <= row[name] <= 1e18
                   for name in ("latency_total_ms", "latency_max_ms")):
                continue
            bins = row.get("latency_bins")
            if not isinstance(bins, list) or len(bins) != 13 or any(type(value) is not int or not 0 <= value <= 10**18 for value in bins) or sum(bins) != row["calls"]:
                continue
            key = (day, model, row["mode"], row["decision_type"])
            if key not in self._groups:
                self._groups[key] = {name: copy.deepcopy(row[name]) for name in (
                    "date_utc", "model", "mode", "decision_type", *counters,
                    "latency_total_ms", "latency_max_ms", "latency_bins",
                )}

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
