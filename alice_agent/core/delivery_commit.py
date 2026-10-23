from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.perception import PerceptionResult
from ..domain.response import AgentResponse
from ..relationship.models import RelationshipState


class DeliveryCommitCoordinator:
    """Replays local writes, with each cumulative update atomically deduplicated.

    There is no model/provider/sender dependency here. The caller holds the user
    data gate and session lock, including during replay before new work.
    """

    def __init__(self, journal, state_manager, memory, sessions, relationship=None):
        self.journal, self.states, self.memory = journal, state_manager, memory
        self.sessions, self.relationship = sessions, relationship
        self.proactive_handler = None

    @staticmethod
    def identity(event):
        raw = json.dumps([event.user_id, event.session_id, event.trace_id], ensure_ascii=False)
        return "delivery_" + hashlib.sha256(raw.encode()).hexdigest()

    def payload(self, event, response, delivered_text):
        pending = response.metadata.get("_alice_pending_commit")
        return {
            "schema_version": 1,
            "event": {key: getattr(event, key) for key in ("event_id", "trace_id", "type", "source", "session_id", "user_id", "timestamp", "text", "event_type")},
            "perception": pending["perception"].prompt_values() if isinstance(pending, dict) else None,
            "plan": asdict(pending["plan"]) if isinstance(pending, dict) else None,
            "selected_ids": [item.id for item in pending["selected_memories"]] if isinstance(pending, dict) else [],
            "delivered_text": delivered_text, "action": response.action,
            "fallback": bool(response.metadata.get("fallback")),
        }

    async def commit(self, event, response, delivered_text):
        if response.metadata.get("_alice_delivery_committed"):
            return
        operation_id = self.identity(event)
        payload = self.payload(event, response, delivered_text)
        observed = None
        if await self.journal.prepare(operation_id, payload):
            observed = await self.apply(operation_id, payload)
        response.metadata.pop("_alice_pending_commit", None)
        response.metadata["_alice_delivery_committed"] = True
        response.metadata.pop("_alice_delivery_pending", None)
        diagnostics = response.metadata.get("turn_diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.update(memory_observe_accepted=observed, delivered_text_length=len(delivered_text))

    async def apply(self, operation_id, payload):
        if payload.get("kind") == "proactive":
            if self.proactive_handler is None:
                raise RuntimeError("proactive delivery recovery is unavailable")
            return await self.proactive_handler(operation_id, payload)
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported delivery intent")
        event = AgentEvent(**payload["event"])
        delivered = payload["delivered_text"]
        observed = False
        if payload["perception"] is not None:
            perception = PerceptionResult(**payload["perception"])
            plan = ActionPlan(**payload["plan"])
            state = await self.states.update_once(event, perception, operation_id)
            relationship = RelationshipState(user_id=event.user_id)
            if self.relationship is not None:
                relationship = await self.relationship.update_once(event, perception, operation_id)
            selected = await self.memory.load_for_delivery(event, payload["selected_ids"])
            await self.memory.record_access(selected, operation_id=operation_id)
            response = AgentResponse(delivered or None, event.trace_id, payload["action"], {"fallback": payload["fallback"]})
            observed = await self.memory.observe(event, perception, state, plan, response, relationship, strict=True)
        await self.sessions.append_and_persist(
            event.session_id, ConversationTurn(event.text or "", delivered or None, event.timestamp, operation_id), strict=True,
        )
        await self.journal.complete(operation_id)
        return observed
