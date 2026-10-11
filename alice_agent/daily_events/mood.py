from __future__ import annotations

from dataclasses import replace
from datetime import date

from ..domain.state import AgentState


_BASELINE = {
    "mood_valence": 0.0,
    "mood_arousal": 0.25,
    "mood_dominance": 0.5,
    "energy": 0.65,
    "stress": 0.2,
    "loneliness": 0.15,
}
_REACTION_KEYS = {
    "mood_valence": "valence",
    "mood_arousal": "arousal",
    "mood_dominance": "dominance",
    "energy": "energy",
    "stress": "stress",
    "loneliness": "loneliness",
}


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


class DailyMoodReducer:
    """Inertia, baseline recovery, then bounded daily-event effects."""

    def reduce(self, state: AgentState, day: str, events: list[dict]) -> AgentState:
        if state.daily_event_day is not None and state.daily_event_day >= day:
            return state
        try:
            elapsed_days = max(1, (date.fromisoformat(day) - date.fromisoformat(state.daily_event_day)).days)
        except (TypeError, ValueError):
            elapsed_days = 1
        retention = 0.75 ** min(elapsed_days, 30)
        values: dict[str, float] = {}
        for field_name, baseline in _BASELINE.items():
            carried = baseline + retention * (float(getattr(state, field_name)) - baseline)
            axis = _REACTION_KEYS[field_name]
            effect = _clamp(
                sum(float(event.get("reaction", {}).get(axis, 0.0)) for event in events),
                -0.30, 0.30,
            )
            low = -1.0 if field_name == "mood_valence" else 0.0
            values[field_name] = _clamp(carried + effect, low, 1.0)
        values["loneliness"] = _clamp(
            values["loneliness"] + min(elapsed_days, 7) * 0.01, 0.0, 1.0
        )
        return replace(
            state,
            **values,
            daily_event_day=day,
            daily_events=[str(item.get("text", ""))[:100] for item in events][:3],
            topic_hooks=[str(item.get("topic_hook", ""))[:60] for item in events if item.get("topic_hook")][:3],
        )
