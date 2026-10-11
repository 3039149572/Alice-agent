from __future__ import annotations

from dataclasses import dataclass, replace

from ..domain.state import AgentState
from ..relationship.models import RelationshipState
from .models import DriveState
from .policies import clamp, saturating_growth


@dataclass(frozen=True, slots=True)
class DriveReducerConfig:
    social_enabled: bool = True
    curiosity_enabled: bool = True
    rest_enabled: bool = True
    social_growth_per_hour: float = 0.025
    curiosity_growth_per_hour: float = 0.018
    rest_growth_per_hour: float = 0.020
    social_user_message_drop: float = 0.22
    curiosity_user_message_drop: float = 0.08
    rest_recovery_per_hour: float = 0.04


class DriveReducer:
    def __init__(self, config: DriveReducerConfig | None = None) -> None:
        self._config = config or DriveReducerConfig()

    def advance(
        self,
        old: DriveState,
        elapsed_seconds: float,
        state: AgentState,
        relationship: RelationshipState,
        *,
        unresolved_context_count: int = 0,
        now: float | None = None,
    ) -> DriveState:
        hours = min(24.0, max(0.0, elapsed_seconds) / 3600.0)
        social = old.social_need
        curiosity = old.curiosity
        rest = old.rest_need
        if self._config.social_enabled:
            relationship_factor = 0.55 + 0.25 * relationship.closeness
            activity_factor = 0.6 if state.activity == "resting" else 1.0
            social = saturating_growth(
                social,
                hours * self._config.social_growth_per_hour
                * relationship_factor * activity_factor,
            )
        if self._config.curiosity_enabled and unresolved_context_count > 0:
            curiosity = saturating_growth(
                curiosity,
                hours * self._config.curiosity_growth_per_hour
                * min(1.5, 0.7 + 0.2 * unresolved_context_count),
            )
        if self._config.rest_enabled:
            if state.activity == "resting":
                rest = clamp(rest - hours * self._config.rest_recovery_per_hour)
            else:
                load = 0.35 + 0.65 * state.mood_arousal
                rest = saturating_growth(
                    rest, hours * self._config.rest_growth_per_hour * load
                )
        return replace(
            old,
            social_need=social,
            curiosity=curiosity,
            rest_need=rest,
            updated_at=now if now is not None else old.updated_at,
            unresolved_context_count=max(0, unresolved_context_count),
        )

    def on_user_message(self, old: DriveState, timestamp: float) -> DriveState:
        effective_timestamp = max(float(timestamp), float(old.updated_at or timestamp))
        return replace(
            old,
            social_need=clamp(
                old.social_need - self._config.social_user_message_drop
            ),
            curiosity=clamp(
                old.curiosity - self._config.curiosity_user_message_drop
            ),
            rest_need=clamp(old.rest_need + 0.01),
            updated_at=effective_timestamp,
            last_user_interaction_at=max(
                float(timestamp), float(old.last_user_interaction_at or timestamp)
            ),
            recent_interaction_count=min(1000, old.recent_interaction_count + 1),
        )
