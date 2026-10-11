from __future__ import annotations

from ..domain.event import AgentEvent
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


class StateReducer:
    """Deterministic, bounded state transition rules."""

    def reduce(
        self,
        old_state: AgentState,
        perception: PerceptionResult,
        event: AgentEvent,
    ) -> AgentState:
        signals = perception.social_signals
        positive = signals.get("affection", 0.0) + signals.get("praise", 0.0) + signals.get("concern", 0.0)
        negative = signals.get("complaint", 0.0) + signals.get("conflict", 0.0)
        valence_delta = _clamp(
            0.12 * perception.valence + 0.04 * positive - 0.06 * negative,
            -0.2,
            0.2,
        )
        stimulus = max(
            signals.get("conflict", 0.0),
            signals.get("vulnerability", 0.0),
            signals.get("teasing", 0.0),
        )
        arousal_delta = _clamp(
            0.15 * (perception.arousal - old_state.mood_arousal) + 0.05 * stimulus,
            -0.2,
            0.2,
        )
        energy_delta = _clamp(
            0.02 * max(signals.get("affection", 0.0), signals.get("praise", 0.0), signals.get("concern", 0.0))
            - 0.03 * max(signals.get("complaint", 0.0), signals.get("conflict", 0.0), signals.get("vulnerability", 0.0)),
            -0.08,
            0.08,
        )

        if signals.get("conflict", 0.0) > 0.55:
            mode = "deescalating"
        elif max(signals.get("vulnerability", 0.0), signals.get("reassurance_seeking", 0.0)) > 0.55:
            mode = "comforting"
        elif signals.get("teasing", 0.0) > 0.6:
            mode = "playful"
        else:
            mode = "casual"

        topic = old_state.current_topic
        if perception.topic and perception.topic != "general" and perception.confidence >= 0.35:
            topic = perception.topic

        return AgentState(
            mood_valence=_clamp(old_state.mood_valence + valence_delta, -1.0, 1.0),
            mood_arousal=_clamp(old_state.mood_arousal + arousal_delta, 0.0, 1.0),
            mood_dominance=_clamp(
                old_state.mood_dominance + 0.04 * (perception.dominance - old_state.mood_dominance),
                0.0, 1.0,
            ),
            energy=_clamp(old_state.energy + energy_delta, 0.0, 1.0),
            stress=_clamp(old_state.stress + 0.035 * negative - 0.02 * positive, 0.0, 1.0),
            loneliness=_clamp(old_state.loneliness - 0.025, 0.0, 1.0),
            activity="chatting" if event.text else old_state.activity,
            current_topic=topic,
            conversation_mode=mode,
            daily_event_day=old_state.daily_event_day,
            daily_events=list(old_state.daily_events),
            topic_hooks=list(old_state.topic_hooks),
        )
