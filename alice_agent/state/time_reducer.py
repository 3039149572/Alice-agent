from __future__ import annotations

from dataclasses import dataclass, replace

from ..domain.state import AgentState


@dataclass(frozen=True, slots=True)
class TimeStateConfig:
    baseline_arousal: float = 0.25
    baseline_energy: float = 0.65
    arousal_return_per_hour: float = 0.08
    energy_return_per_hour: float = 0.04


class TimeStateReducer:
    """Predictable time-only state movement; it never interprets user intent."""

    def __init__(self, config: TimeStateConfig | None = None) -> None:
        self._config = config or TimeStateConfig()

    @staticmethod
    def _move_toward(value: float, target: float, step: float) -> float:
        if value < target:
            return min(target, value + step)
        return max(target, value - step)

    def reduce(self, state: AgentState, elapsed_seconds: float) -> AgentState:
        hours = min(24.0, max(0.0, float(elapsed_seconds)) / 3600.0)
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
        activity = "idle" if hours >= 0.25 and state.activity == "chatting" else state.activity
        return replace(
            state,
            mood_arousal=min(1.0, max(0.0, arousal)),
            energy=min(1.0, max(0.0, energy)),
            activity=activity,
        )
