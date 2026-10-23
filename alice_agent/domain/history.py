from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    user_text: str
    assistant_text: str | None
    timestamp: float
    delivery_id: str | None = None
