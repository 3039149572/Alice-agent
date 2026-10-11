from __future__ import annotations

from typing import Any

from ..domain.action import ActionPlan, VALID_ACTIONS
from ..perception.parser import clamp, clean_text, parse_json_object


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:80] if value else None


class ActionPlanParser:
    def parse(self, raw: str | None, available_memory_ids: set[str] | None = None) -> ActionPlan:
        data = parse_json_object(raw)
        if data is None:
            return ActionPlan.fallback()
        action = clean_text(data.get("action"), "reply", max_length=40)
        if action not in VALID_ACTIONS:
            action = "reply"
        raw_ids = data.get("memory_ids", [])
        allowed = available_memory_ids or set()
        memory_ids = []
        if isinstance(raw_ids, list):
            memory_ids = [
                value for value in raw_ids if isinstance(value, str) and value in allowed
            ]
        return ActionPlan(
            action=action,
            intent=clean_text(data.get("intent"), "respond"),
            response_goal=clean_text(data.get("response_goal"), "回应当前消息", max_length=240),
            emotion=clean_text(data.get("emotion"), "calm", max_length=80),
            energy=clamp(data.get("energy"), 0.0, 1.0, 0.5),
            memory_ids=memory_ids,
            expression=_optional_text(data.get("expression")),
            voice_style=_optional_text(data.get("voice_style")),
            tone=clean_text(data.get("tone"), "neutral", max_length=40),
            response_length=clean_text(data.get("response_length"), "medium", max_length=20),
            directness=clamp(data.get("directness"), 0.0, 1.0, 0.5),
            ask_followup=bool(data.get("ask_followup", False)),
            emotion_style=clean_text(data.get("emotion_style"), clean_text(data.get("emotion"), "calm"), max_length=80),
            initiative_level=clamp(data.get("initiative_level"), 0.0, 1.0, 0.0),
            requires_deep_reasoning=bool(data.get("requires_deep_reasoning", False)),
        )
