from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


def _unit(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


class CandidateActionType(StrEnum):
    DO_NOTHING = "do_nothing"
    PROACTIVE_MESSAGE = "proactive_message"
    STATE_ONLY_UPDATE = "state_only_update"
    MEMORY_FOLLOW_UP = "memory_follow_up"


class ActionStatus(StrEnum):
    CANDIDATE = "candidate"
    PLANNED = "planned"
    EXECUTING = "executing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(slots=True)
class CandidateAction:
    action_type: CandidateActionType
    reason: str
    urgency: float = 0.0
    relevance: float = 0.0
    interruption_cost: float = 0.7
    source_event_ids: list[str] = field(default_factory=list)
    topic: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    action_id: str = field(default_factory=lambda: f"act_{uuid4().hex}")
    created_at: float = 0.0
    expires_at: float | None = None
    status: ActionStatus = ActionStatus.CANDIDATE

    def __post_init__(self) -> None:
        self.urgency = _unit(self.urgency)
        self.relevance = _unit(self.relevance)
        self.interruption_cost = _unit(self.interruption_cost)


@dataclass(slots=True)
class ProactiveActionPlan:
    action: str = "do_nothing"
    reason: str = "insufficient reason to interrupt"
    topic: str | None = None
    response_goal: str | None = None
    memory_ids: list[str] = field(default_factory=list)

    @classmethod
    def do_nothing(cls, reason: str) -> "ProactiveActionPlan":
        return cls(reason=reason)


@dataclass(slots=True)
class UserAvailability:
    last_interaction_time: float | None = None
    known_active_session: bool = False
    manual_status: str = "unknown"
    desktop_idle: bool | None = None
    screen_locked: bool | None = None
    gaming: bool | None = None
    sleeping: bool | None = None


@dataclass(slots=True)
class ScheduledContextEvent:
    id: str
    user_id: str
    session_id: str
    event_type: str
    title: str
    summary: str
    expected_at: float
    followup_start: float
    followup_end: float
    related_memory_ids: list[str] = field(default_factory=list)
    status: str = "scheduled"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionDecision:
    decision: str
    score: float
    reason: str


@dataclass(slots=True)
class LoopMetrics:
    total_ticks: int = 0
    candidate_actions: int = 0
    proactive_planner_calls: int = 0
    proactive_messages_sent: int = 0
    do_nothing_count: int = 0
    blocked_by_cooldown: int = 0
    blocked_by_daily_budget: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            name: int(getattr(self, name))
            for name in self.__dataclass_fields__
        }
