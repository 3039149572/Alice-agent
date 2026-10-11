from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


@dataclass(slots=True)
class DriveState:
    session_id: str
    user_id: str
    social_need: float = 0.1
    curiosity: float = 0.0
    rest_need: float = 0.0
    updated_at: float | None = None
    last_user_interaction_at: float | None = None
    recent_interaction_count: int = 0
    unresolved_context_count: int = 0

    def __post_init__(self) -> None:
        self.social_need = _clamp(self.social_need)
        self.curiosity = _clamp(self.curiosity)
        self.rest_need = _clamp(self.rest_need)
        self.recent_interaction_count = max(0, int(self.recent_interaction_count))
        self.unresolved_context_count = max(0, int(self.unresolved_context_count))
