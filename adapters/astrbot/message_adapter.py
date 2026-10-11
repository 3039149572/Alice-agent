from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from ...alice_agent.domain.event import AgentEvent


class AstrBotMessageAdapter:
    @staticmethod
    def new_trace_id(now: float | None = None) -> str:
        moment = datetime.fromtimestamp(now or time.time())
        return f"alice_{moment:%Y%m%d}_{uuid.uuid4().hex[:8]}"

    def to_agent_event(self, event: Any) -> AgentEvent:
        message_obj = getattr(event, "message_obj", None)
        timestamp = float(getattr(message_obj, "timestamp", None) or getattr(event, "created_at", time.time()))
        event_id = str(getattr(message_obj, "message_id", "") or uuid.uuid4().hex)
        message_type = getattr(event.get_message_type(), "value", None)
        if message_type is None:
            message_type = str(event.get_message_type())
        metadata = {
            "platform_id": str(event.get_platform_id()),
            "sender_name": str(event.get_sender_name() or ""),
            "group_id": str(event.get_group_id() or ""),
            "merge_safe": self._is_text_only(event, message_obj),
        }
        return AgentEvent(
            event_id=event_id,
            trace_id=self.new_trace_id(timestamp),
            type=str(message_type),
            source=str(event.get_platform_name()),
            session_id=str(event.unified_msg_origin),
            user_id=str(event.get_sender_id()),
            timestamp=timestamp,
            text=str(event.message_str).strip() if event.message_str is not None else None,
            metadata=metadata,
            event_type="USER_MESSAGE",
            payload={"text": str(event.message_str or "")},
        )

    @staticmethod
    def _is_text_only(event: Any, message_obj: Any) -> bool:
        """Only merge plain text; images/audio/mentions/forwards keep their boundaries."""
        try:
            getter = getattr(event, "get_messages", None)
            components = getter() if callable(getter) else getattr(message_obj, "message", None)
        except Exception:
            return False
        if components is None:
            return bool(str(getattr(event, "message_str", "") or "").strip())
        try:
            values = list(components)
        except TypeError:
            return False
        return bool(values) and all(type(component).__name__ == "Plain" for component in values)
