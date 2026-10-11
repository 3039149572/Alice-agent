from __future__ import annotations

from datetime import datetime, time

from .models import UserAvailability


def active_conversation(availability: UserAvailability, now: float, window: float) -> bool:
    last = availability.last_interaction_time
    return last is not None and now - last < max(0.0, window)


def interruption_cost(availability: UserAvailability) -> float:
    if availability.manual_status in {"do_not_disturb", "unavailable"}:
        return 1.0
    if availability.sleeping or availability.screen_locked:
        return 1.0
    if availability.gaming:
        return 0.9
    if availability.manual_status == "available":
        return 0.25
    return 0.7


class LocalTimeWindow:
    """A daily wall-clock window interpreted in the machine's local timezone."""

    def __init__(self, start: str = "09:00", end: str = "21:00") -> None:
        parsed_start = self._parse(start)
        parsed_end = self._parse(end)
        if parsed_start is None or parsed_end is None:
            parsed_start, parsed_end = time(9, 0), time(21, 0)
        self.start = parsed_start
        self.end = parsed_end

    @staticmethod
    def _parse(value: str) -> time | None:
        try:
            parsed = time.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is not None:
            return None
        return parsed

    def contains(self, timestamp: float) -> bool:
        current = datetime.fromtimestamp(float(timestamp)).time()
        if self.start <= self.end:
            return self.start <= current <= self.end
        return current >= self.start or current <= self.end
