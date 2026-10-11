from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentResponse:
    text: str | None
    trace_id: str
    action: str = "reply"
    metadata: dict[str, Any] = field(default_factory=dict)
