from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentEvent:
    event_id: str
    trace_id: str
    type: str
    source: str
    session_id: str
    user_id: str
    timestamp: float
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    event_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_type:
            self.event_type = self.type
