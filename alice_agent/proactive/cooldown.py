from __future__ import annotations

from dataclasses import dataclass
@dataclass(frozen=True, slots=True)
class CooldownConfig:
    min_interval: float = 21600.0
    same_topic_interval: float = 86400.0
    daily_budget: int = 1


class CooldownManager:
    def __init__(self, store: object, config: CooldownConfig | None = None) -> None:
        self._store = store
        self._config = config or CooldownConfig()

    @staticmethod
    def _window_start(timestamp: float) -> float:
        # A rolling window is stricter than a calendar-day reset at midnight.
        return float(timestamp) - 86400.0

    async def check(
        self, user_id: str, topic: str | None, now: float,
        session_id: str | None = None,
    ) -> tuple[bool, str]:
        stats = await self._store.send_stats(
            user_id, topic, self._window_start(now), session_id
        )
        if int(stats["daily_count"] or 0) >= self._config.daily_budget:
            return False, "daily_budget"
        global_last = stats["global_last"]
        user_last = stats["user_last"]
        topic_last = stats["topic_last"]
        if global_last is not None and now - float(global_last) < self._config.min_interval:
            return False, "global_cooldown"
        if user_last is not None and now - float(user_last) < self._config.min_interval:
            return False, "user_cooldown"
        if topic_last is not None and now - float(topic_last) < self._config.same_topic_interval:
            return False, "same_topic_cooldown"
        return True, "allowed"

    async def record(self, user_id: str, session_id: str, topic: str | None, now: float) -> None:
        await self._store.record_send(user_id, session_id, topic, now)
