from __future__ import annotations

import math

from ..drives.models import DriveState
from .models import CandidateAction, CandidateActionType, ScheduledContextEvent, UserAvailability
from .policies import LocalTimeWindow, interruption_cost


class ProactiveEvaluator:
    def __init__(self, window_start: str = "09:00", window_end: str = "21:00") -> None:
        self._send_window = LocalTimeWindow(window_start, window_end)

    def in_send_window(self, timestamp: float) -> bool:
        return self._send_window.contains(timestamp)

    def evaluate(
        self,
        drive: DriveState,
        availability: UserAvailability,
        now: float,
        scheduled: ScheduledContextEvent | None = None,
        daily_event: dict[str, object] | None = None,
    ) -> CandidateAction:
        cost = interruption_cost(availability)
        if scheduled is not None:
            return CandidateAction(
                action_type=CandidateActionType.MEMORY_FOLLOW_UP,
                reason="a relevant scheduled context entered its follow-up window",
                urgency=0.65,
                relevance=0.9,
                interruption_cost=cost,
                source_event_ids=[scheduled.id],
                topic=scheduled.title,
                metadata={
                    "summary": scheduled.summary,
                    "related_memory_ids": list(scheduled.related_memory_ids),
                },
                created_at=now,
                expires_at=scheduled.followup_end,
            )
        if daily_event is not None and self.in_send_window(now):
            event_id = str(daily_event.get("event_id", "")).strip()
            text = str(daily_event.get("text", "")).strip()
            topic = str(daily_event.get("topic_hook", "")).strip()
            if event_id and text and topic:
                event_day = str(daily_event.get("day", ""))
                try:
                    importance = float(daily_event.get("importance", 0.35))
                except (TypeError, ValueError, OverflowError):
                    importance = 0.35
                if not math.isfinite(importance):
                    importance = 0.35
                importance = min(1.0, max(0.0, importance))
                return CandidateAction(
                    action_type=CandidateActionType.PROACTIVE_MESSAGE,
                    reason="a concrete daily experience may be worth sharing",
                    urgency=0.25,
                    relevance=0.62 + 0.2 * importance,
                    interruption_cost=cost,
                    source_event_ids=[event_id],
                    topic=topic,
                    metadata={
                        "summary": text,
                        "daily_event_id": event_id,
                        "daily_event_day": event_day,
                        "topic_hook": topic,
                    },
                    created_at=now,
                )
        if drive.curiosity >= 0.8 and drive.unresolved_context_count > 0:
            return CandidateAction(
                action_type=CandidateActionType.PROACTIVE_MESSAGE,
                reason="unresolved context raised curiosity but lacks a timed trigger",
                urgency=0.25,
                relevance=0.2,
                interruption_cost=cost,
                topic="unfinished-topic",
                created_at=now,
                expires_at=now + 900.0,
            )
        return CandidateAction(
            action_type=CandidateActionType.DO_NOTHING,
            reason="no concrete context worth interrupting the user for",
            interruption_cost=cost,
            created_at=now,
        )
