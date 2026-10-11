from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentConfig:
    enabled: bool = True
    debug: bool = False
    llm_timeout: float = 30.0
    perception_enabled: bool = True
    planner_enabled: bool = True
    memory_enabled: bool = True
    memory_metadata_enabled: bool = True
    memory_backend: str = "local_sqlite"
    history_limit: int = 8
    session_persistence_enabled: bool = True
    history_restore_max_age_hours: float = 72.0
    memory_recall_limit: int = 5
    memory_selected_limit: int = 3
    memory_importance_threshold: float = 0.6
    memory_min_relevance: float = 0.12
    memory_semantic_weight: float = 0.65
    memory_importance_weight: float = 0.15
    memory_emotion_weight: float = 0.07
    memory_relationship_weight: float = 0.08
    memory_recency_weight: float = 0.05
    relationship_enabled: bool = True
    relationship_max_delta_per_turn: float = 0.02
    agent_loop_enabled: bool = True
    agent_tick_interval: float = 30.0
    daily_events_enabled: bool = False
    daily_event_max: int = 2
    daily_event_llm_enabled: bool = True
    daily_event_jev_enabled: bool = True
    response_timing_enabled: bool = True
    response_wait_quiet_seconds: float = 0.8
    response_wait_max_seconds: float = 3.0
    persona_profile: dict[str, Any] = field(default_factory=dict)
    drive_social_enabled: bool = True
    drive_curiosity_enabled: bool = True
    drive_rest_enabled: bool = True
    drive_social_growth_per_hour: float = 0.025
    drive_curiosity_growth_per_hour: float = 0.035
    drive_rest_growth_per_hour: float = 0.04
    proactive_enabled: bool = True
    proactive_score_threshold: float = 0.72
    proactive_daily_score_threshold: float = 0.45
    proactive_defer_threshold: float = 0.60
    proactive_min_interval: float = 21600.0
    proactive_same_topic_interval: float = 86400.0
    proactive_daily_budget: int = 1
    proactive_window_start: str = "09:00"
    proactive_window_end: str = "21:00"
    proactive_daily_attempt_limit: int = 2
    active_conversation_window: float = 900.0
    planner_timeout: float = 20.0
    proactive_social_weight: float = 0.18
    proactive_curiosity_weight: float = 0.24
    proactive_relationship_weight: float = 0.15
    proactive_relevance_weight: float = 0.55
    proactive_urgency_weight: float = 0.20
    proactive_interruption_weight: float = 0.15
    proactive_rest_weight: float = 0.08
    jev_enabled: bool = False
    jev_mode: str = "shadow"
    jev_endpoint: str = ""
    jev_api_key: str = field(default="", repr=False)
    jev_model: str = "jev-latest"
    jev_timeout_ms: int = 2500
    jev_auto_threshold: float = 0.85
    jev_review_threshold: float = 0.80
    jev_perception_enabled: bool = True
    jev_memory_enabled: bool = True
    jev_planner_enabled: bool = True
    jev_proactive_enabled: bool = True
    jev_memory_relevance_blend: float = 0.25
    fallback_text: str = "抱歉，我刚刚没能组织好回应。可以再说一遍吗？"

    def __post_init__(self) -> None:
        object.__setattr__(self, "llm_timeout", max(1.0, float(self.llm_timeout)))
        object.__setattr__(self, "history_limit", max(0, int(self.history_limit)))
        object.__setattr__(self, "session_persistence_enabled", bool(self.session_persistence_enabled))
        try:
            history_age = float(self.history_restore_max_age_hours)
        except (TypeError, ValueError, OverflowError):
            history_age = 72.0
        if not math.isfinite(history_age):
            history_age = 72.0
        object.__setattr__(
            self, "history_restore_max_age_hours", min(168.0, max(1.0, history_age))
        )
        object.__setattr__(self, "memory_recall_limit", max(0, int(self.memory_recall_limit)))
        object.__setattr__(self, "memory_selected_limit", max(0, int(self.memory_selected_limit)))
        object.__setattr__(
            self,
            "memory_importance_threshold",
            min(1.0, max(0.0, float(self.memory_importance_threshold))),
        )
        for name in (
            "memory_min_relevance", "memory_semantic_weight",
            "memory_importance_weight", "memory_emotion_weight",
            "memory_relationship_weight", "memory_recency_weight",
        ):
            object.__setattr__(
                self, name, min(1.0, max(0.0, float(getattr(self, name))))
            )
        object.__setattr__(
            self,
            "relationship_max_delta_per_turn",
            min(0.1, max(0.001, float(self.relationship_max_delta_per_turn))),
        )
        object.__setattr__(self, "agent_tick_interval", max(1.0, float(self.agent_tick_interval)))
        object.__setattr__(self, "daily_event_max", min(3, max(0, int(self.daily_event_max))))
        quiet_seconds = min(2.0, max(0.1, float(self.response_wait_quiet_seconds)))
        object.__setattr__(self, "response_wait_quiet_seconds", quiet_seconds)
        object.__setattr__(
            self, "response_wait_max_seconds",
            min(8.0, max(quiet_seconds, float(self.response_wait_max_seconds))),
        )
        object.__setattr__(self, "planner_timeout", max(1.0, float(self.planner_timeout)))
        for name in (
            "drive_social_growth_per_hour", "drive_curiosity_growth_per_hour",
            "drive_rest_growth_per_hour", "proactive_score_threshold",
            "proactive_daily_score_threshold",
            "proactive_defer_threshold", "proactive_social_weight",
            "proactive_curiosity_weight", "proactive_relationship_weight",
            "proactive_relevance_weight", "proactive_interruption_weight",
            "proactive_rest_weight", "proactive_urgency_weight",
        ):
            object.__setattr__(self, name, min(1.0, max(0.0, float(getattr(self, name)))))
        for name in (
            "proactive_min_interval", "proactive_same_topic_interval",
            "active_conversation_window",
        ):
            object.__setattr__(self, name, max(0.0, float(getattr(self, name))))
        object.__setattr__(self, "proactive_daily_budget", max(0, int(self.proactive_daily_budget)))
        object.__setattr__(self, "proactive_daily_attempt_limit", max(0, int(self.proactive_daily_attempt_limit)))
        mode = str(self.jev_mode).strip().lower()
        if mode not in {"off", "shadow", "hybrid", "primary"}:
            mode = "shadow"
        object.__setattr__(self, "jev_mode", mode)
        object.__setattr__(self, "jev_timeout_ms", max(100, int(self.jev_timeout_ms)))
        object.__setattr__(
            self, "jev_auto_threshold",
            min(1.0, max(0.0, float(self.jev_auto_threshold))),
        )
        object.__setattr__(
            self, "jev_review_threshold",
            min(float(self.jev_auto_threshold), max(0.0, float(self.jev_review_threshold))),
        )
        object.__setattr__(
            self, "jev_memory_relevance_blend",
            min(1.0, max(0.0, float(self.jev_memory_relevance_blend))),
        )
