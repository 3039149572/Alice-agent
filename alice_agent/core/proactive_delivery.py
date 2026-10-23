from __future__ import annotations

from dataclasses import asdict

from ..async_utils import finish_before_cancelling
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn


class ProactiveDeliveryCommitter:
    """Record confirmed proactive text, cooldown and action state without resending."""

    def __init__(self, journal, store, memory, sessions):
        self.journal, self.store, self.memory, self.sessions = journal, store, memory, sessions

    def payload(self, event, candidate, text, full_text, selected, day, daily_id, scheduled_id):
        action = asdict(candidate)
        action["status"] = "completed" if text == full_text else "partial"
        return {
            "schema_version": 2, "kind": "proactive",
            "event": {key: getattr(event, key) for key in ("event_id", "trace_id", "type", "source", "session_id", "user_id", "timestamp", "text", "event_type")},
            "action": action, "delivered_text": text, "selected_ids": [item.id for item in selected],
            "daily_day": day, "daily_event_id": daily_id, "scheduled_id": scheduled_id,
        }

    async def commit(self, payload):
        if not payload["delivered_text"]:
            return
        operation_id = "proactive_" + payload["action"]["action_id"]
        async def apply():
            if await self.journal.prepare(operation_id, payload):
                await self.apply(operation_id, payload)
        await finish_before_cancelling(apply())

    async def apply(self, operation_id, payload):
        event = AgentEvent(**payload["event"])
        await self.store.commit_delivery_once(operation_id, payload)
        selected = await self.memory.load_for_delivery(event, payload["selected_ids"])
        await self.memory.record_access(selected, operation_id=operation_id)
        await self.sessions.append_and_persist(event.session_id, ConversationTurn("", payload["delivered_text"], event.timestamp, operation_id), strict=True)
        await self.journal.complete(operation_id)
