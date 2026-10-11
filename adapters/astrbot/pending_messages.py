from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import time
from typing import Any

from ...alice_agent.decision.response_timing import ReplyTimingDecision
from ...alice_agent.domain.event import AgentEvent


@dataclass(slots=True)
class PendingMessage:
    agent_event: AgentEvent
    framework_event: Any
    merge_safe: bool
    revision: int
    timing: ReplyTimingDecision | None = None
    timing_ready: asyncio.Event = field(default_factory=asyncio.Event)
    classification_task: asyncio.Task | None = None


@dataclass(slots=True)
class PendingReplyBatch:
    session_id: str
    items: list[PendingMessage]
    phase: str = "classifying"
    created_at: float = field(default_factory=time.monotonic)
    last_input_at: float = field(default_factory=time.monotonic)
    version: int = 1
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    turn_changed: asyncio.Event = field(default_factory=asyncio.Event)
    boundary: bool = False
    max_messages: int = 8

    def can_join(self, item: PendingMessage) -> bool:
        return (
            self.phase in {"classifying", "waiting", "processing"}
            and not self.boundary
            and item.merge_safe
            and all(existing.merge_safe for existing in self.items)
            and len(self.items) < self.max_messages
        )

    def add(self, item: PendingMessage, *, boundary: bool = False) -> None:
        self.items.append(item)
        self.last_input_at = time.monotonic()
        self.version += 1
        self.boundary = self.boundary or boundary
        self.changed.set()

    async def wait_for_classification(self) -> int:
        while True:
            version = self.version
            snapshot = tuple(self.items)
            await asyncio.gather(*(item.timing_ready.wait() for item in snapshot))
            if version == self.version:
                return version

    def action(self) -> str:
        decided = [item.timing for item in self.items if item.timing is not None]
        if not decided:
            return "reply_now"
        if any(
            item.timing is not None
            and item.timing.diagnostic.get("rule_action") == "ignore"
            for item in self.items
        ):
            return "ignore"
        last_local_wait = max(
            (index for index, item in enumerate(self.items)
             if item.timing is not None and item.timing.diagnostic.get("explicit_wait")),
            default=-1,
        )
        if last_local_wait >= 0:
            if any(
                item.timing is not None
                and item.timing.diagnostic.get("protected_question")
                for item in self.items[last_local_wait + 1:]
            ):
                return "reply_now"
            return "wait"
        if decided[-1].action == "ignore" and decided[-1].source == "jev":
            if not any(
                item.timing is not None
                and item.timing.diagnostic.get("protected_question")
                for item in self.items
            ):
                return "ignore"
        if any(
            item.timing is not None
            and item.timing.diagnostic.get("protected_question")
            for item in self.items
        ):
            return "reply_now"
        if decided[-1].action == "reply_now":
            return "reply_now"
        return "wait"

    def timing_diagnostics(self) -> list[dict[str, Any]]:
        return [
            {
                "event_id": item.agent_event.event_id,
                **(item.timing.diagnostic if item.timing is not None else {"status": "pending"}),
            }
            for item in self.items
        ]


class PendingReplyCoordinator:
    """Groups only bounded, text-only continuation messages per session."""

    def __init__(self, max_messages: int = 8) -> None:
        self._queues: defaultdict[str, deque[PendingReplyBatch]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._max_messages = max(1, int(max_messages))

    async def submit(
        self,
        item: PendingMessage,
        *,
        boundary: bool = False,
        cancel_current: bool = False,
    ) -> tuple[PendingReplyBatch, bool]:
        async with self._lock:
            queue = self._queues[item.agent_event.session_id]
            if queue and (not boundary or cancel_current) and queue[-1].can_join(item):
                batch = queue[-1]
                batch.add(item, boundary=boundary)
                return batch, False
            batch = PendingReplyBatch(
                session_id=item.agent_event.session_id,
                items=[item],
                boundary=boundary,
                max_messages=self._max_messages,
            )
            queue.append(batch)
            return batch, True

    async def finish(self, batch: PendingReplyBatch) -> None:
        async with self._lock:
            queue = self._queues.get(batch.session_id)
            if queue is None:
                return
            try:
                queue.remove(batch)
            except ValueError:
                return
            batch.phase = "done"
            batch.changed.set()
            if queue:
                queue[0].turn_changed.set()
            if not queue:
                self._queues.pop(batch.session_id, None)

    async def wait_turn(self, batch: PendingReplyBatch) -> bool:
        """Wait for FIFO position without holding the coordinator lock."""
        while True:
            async with self._lock:
                queue = self._queues.get(batch.session_id)
                if queue is None or batch not in queue or batch.phase == "cancelled":
                    return False
                if queue[0] is batch:
                    return True
                batch.turn_changed.clear()
            await batch.turn_changed.wait()

    async def cancel_all(self) -> None:
        async with self._lock:
            for queue in self._queues.values():
                for batch in queue:
                    batch.phase = "cancelled"
                    batch.changed.set()
                    batch.turn_changed.set()
