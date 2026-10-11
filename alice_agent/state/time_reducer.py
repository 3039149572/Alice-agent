from __future__ import annotations

from dataclasses import dataclass, replace

from ..domain.state import AgentState


@dataclass(frozen=True, slots=True)
class TimeStateConfig:
    baseline_valence: float = 0.0
    baseline_arousal: float = 0.25
    baseline_energy: float = 0.65
    arousal_return_per_hour: float = 0.08
    energy_return_per_hour: float = 0.04
    valence_return_per_hour: float = 0.02
    idle_after_seconds: float = 900.0
    rest_after_seconds: float = 1800.0


class TimeStateReducer:
    """Predictable time-only state movement; it never interprets user intent."""

    def __init__(self, config: TimeStateConfig | None = None) -> None:
        self._config = config or TimeStateConfig()

    @staticmethod
    def _move_toward(value: float, target: float, step: float) -> float:
        if value < target:
            return min(target, value + step)
        return max(target, value - step)

    def reduce(
        self, state: AgentState, elapsed_seconds: float, *,
        inactivity_seconds: float | None = None,
        rest_need: float = 0.0,
    ) -> AgentState:
        hours = min(24.0, max(0.0, float(elapsed_seconds)) / 3600.0)
        inactivity = max(0.0, float(
            elapsed_seconds if inactivity_seconds is None else inactivity_seconds
        ))
        valence = self._move_toward(
            state.mood_valence, self._config.baseline_valence,
            self._config.valence_return_per_hour * hours,
        )
        arousal = self._move_toward(
            state.mood_arousal,
            self._config.baseline_arousal,
            self._config.arousal_return_per_hour * hours,
        )
        energy = self._move_toward(
            state.energy,
            self._config.baseline_energy,
            self._config.energy_return_per_hour * hours,
        )
        activity = state.activity
        if activity == "resting":
            if inactivity < self._config.idle_after_seconds or (rest_need <= 0.20 and energy >= 0.60):
                activity = "idle"
        elif inactivity >= self._config.rest_after_seconds and (rest_need >= 0.60 or energy <= 0.35):
            activity = "resting"
        elif inactivity >= self._config.idle_after_seconds and activity == "chatting":
            activity = "idle"
        return replace(
            state,
            mood_valence=min(1.0, max(-1.0, valence)),
            mood_arousal=min(1.0, max(0.0, arousal)),
            energy=min(1.0, max(0.0, energy)),
            activity=activity,
        )
