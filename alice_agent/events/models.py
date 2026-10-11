from __future__ import annotations

import uuid
from typing import Any

from ..domain.event import AgentEvent
from .types import EventType


def new_event(
    event_type: EventType,
    timestamp: float,
    *,
    source: str = "alice_core",
    session_id: str = "",
    user_id: str = "",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AgentEvent:
    event_id = f"evt_{uuid.uuid4().hex}"
    return AgentEvent(
        event_id=event_id,
        trace_id=trace_id or f"alice_loop_{uuid.uuid4().hex[:12]}",
        type=event_type.value,
        source=source,
        session_id=session_id,
        user_id=user_id,
        timestamp=timestamp,
        metadata=metadata or {},
        event_type=event_type.value,
        payload=payload or {},
    )
