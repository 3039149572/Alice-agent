from __future__ import annotations

import json
import math
from typing import Any

from ..domain.perception import PerceptionResult, SOCIAL_SIGNAL_KEYS


def clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, numeric)) if math.isfinite(numeric) else default


def _valid_number(value: Any, low: float, high: float) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and low <= number <= high


def parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def clean_text(value: Any, default: str, max_length: int = 120) -> str:
    if not isinstance(value, str):
        return default
    value = value.strip()
    return value[:max_length] if value else default


class PerceptionParser:
    def parse(self, raw: str | None) -> PerceptionResult:
        data = parse_json_object(raw)
        if data is None:
            return PerceptionResult.fallback()

        defaults = PerceptionResult.fallback()
        raw_signals = data.get("social_signals")
        if not isinstance(raw_signals, dict):
            raw_signals = {}
        signals = {
            key: clamp(raw_signals.get(key), 0.0, 1.0, 0.0)
            for key in SOCIAL_SIGNAL_KEYS
        }
        candidate = data.get("memory_candidate", defaults.memory_candidate)
        if not isinstance(candidate, bool):
            candidate = defaults.memory_candidate
        numeric_evidence = {
            key for key, low, high in (
                ("valence", -1.0, 1.0), ("arousal", 0.0, 1.0),
                ("dominance", 0.0, 1.0), ("importance", 0.0, 1.0),
            ) if _valid_number(data.get(key), low, high)
        }
        numeric_evidence.update(
            f"social_{key}" for key in SOCIAL_SIGNAL_KEYS
            if _valid_number(raw_signals.get(key), 0.0, 1.0)
        )
        return PerceptionResult(
            intent=clean_text(data.get("intent"), defaults.intent),
            topic=clean_text(data.get("topic"), defaults.topic),
            user_emotion=clean_text(data.get("user_emotion"), defaults.user_emotion),
            valence=clamp(data.get("valence"), -1.0, 1.0, defaults.valence),
            arousal=clamp(data.get("arousal"), 0.0, 1.0, defaults.arousal),
            dominance=clamp(data.get("dominance"), 0.0, 1.0, defaults.dominance),
            social_signals=signals,
            importance=clamp(data.get("importance"), 0.0, 1.0, defaults.importance),
            memory_candidate=candidate,
            confidence=clamp(data.get("confidence"), 0.0, 1.0, defaults.confidence),
            numeric_evidence=frozenset(numeric_evidence),
        )
