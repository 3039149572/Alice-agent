from __future__ import annotations

import asyncio
import time
from contextlib import aclosing

from astrbot.api.event import AstrMessageEvent
from ...alice_agent.decision.response_timing import ReplyTimingDecision, ResponseTimingService
from ...alice_agent.domain.event import AgentEvent
from ...alice_agent.domain.response import AgentResponse
from .pending_messages import PendingMessage, PendingReplyBatch


class ReactiveMessageMixin:
    """AstrBot intake, revision checks, merge timing and ordered reply handling.

    The composition root provides config, message adapter, agent/loop, pending
    coordinator, data gate, lifecycle task sets, logger and delivery methods.
    Arrival registration stays outside the data/send locks.
    """

    def _begin_input_registration(self, session_id: str, event_id: str) -> bool:
        key = (session_id, event_id)
        if key in self._recent_event_ids:
            return False
        self._recent_event_ids[key] = None
        while len(self._recent_event_ids) > 4096:
            self._recent_event_ids.popitem(last=False)
        if self._registration_pending[session_id] == 0:
            self._registrations_ready[session_id].clear()
        self._registration_pending[session_id] += 1
        # This synchronous epoch bump is deliberately before every await: it
        # invalidates any old generation before database or Jev latency.
        self._reactive_epoch[session_id] += 1
        return True

    def _finish_input_registration(self, session_id: str) -> None:
        self._registration_pending[session_id] = max(
            0, self._registration_pending[session_id] - 1
        )
        if self._registration_pending[session_id] == 0:
            self._registrations_ready[session_id].set()

    async def _prepare_pending_message(
        self, framework_event: AstrMessageEvent
    ) -> tuple[PendingReplyBatch, PendingMessage, bool] | None:
        agent_event = self._message_adapter.to_agent_event(framework_event)
        if getattr(self, "_data_service", None) is not None:
            self._data_service.stamp(agent_event)
        session_id = agent_event.session_id
        if not self._begin_input_registration(session_id, agent_event.event_id):
            framework_event.stop_event()
            return None
        item: PendingMessage | None = None
        try:
            revision = await self._agent_loop.register_user_event(agent_event)
            if revision is None:
                framework_event.stop_event()
                return None
            item = PendingMessage(
                agent_event=agent_event,
                framework_event=framework_event,
                merge_safe=bool(agent_event.metadata.get("merge_safe", False)),
                revision=revision,
            )
            batch, is_owner = await self._pending_replies.submit(
                item,
                boundary=ResponseTimingService.has_batch_boundary(agent_event.text or ""),
                cancel_current=ResponseTimingService.is_no_reply_command(agent_event.text or ""),
            )
        finally:
            self._finish_input_registration(session_id)
        try:
            await self._agent_loop.apply_user_activity(agent_event)
        except Exception:
            self.logger.exception(
                f"[{agent_event.trace_id}] Alice user activity update failed safely"
            )
        classifier = asyncio.create_task(self._classify_pending_item(batch, item))
        item.classification_task = classifier
        self._classification_tasks.add(classifier)
        classifier.add_done_callback(self._classification_tasks.discard)
        return batch, item, is_owner

    async def _classify_pending_item(
        self, batch: PendingReplyBatch, item: PendingMessage
    ) -> None:
        try:
            async with self._timing_locks[batch.session_id]:
                if item.timing is not None or batch.phase == "cancelled":
                    return
                messages = [candidate.agent_event.text or "" for candidate in batch.items]
                history = await self._session_manager.load_history(batch.session_id)
                decision = await self._timing_service.decide(
                    item.agent_event,
                    history=history,
                    current_messages=messages,
                )
                if item.timing is None:
                    item.timing = decision
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                f"[{item.agent_event.trace_id}] response timing failed; using local fallback ({type(exc).__name__})"
            )
            if item.timing is None:
                item.timing = self._timing_service.local_fallback(item.agent_event)
        finally:
            if item.timing is None and batch.phase != "cancelled":
                item.timing = self._timing_service.local_fallback(item.agent_event)
            item.timing_ready.set()

    async def _await_pending_registrations(self, session_id: str) -> None:
        if self._registration_pending[session_id] > 0:
            await self._registrations_ready[session_id].wait()

    async def _resolve_batch_timing(
        self, batch: PendingReplyBatch
    ) -> tuple[str, bool, float]:
        hard_deadline = batch.created_at + self._config.response_wait_max_seconds
        while not self._closing:
            remaining = hard_deadline - time.monotonic()
            try:
                await asyncio.wait_for(
                    batch.wait_for_classification(), timeout=max(0.001, remaining)
                )
            except asyncio.TimeoutError:
                # Jev latency cannot extend the first-message hard deadline.
                # Freeze unfinished classifications to their deterministic
                # local rules and cancel their late model requests.
                for item in batch.items:
                    if item.timing is None:
                        item.timing = self._timing_service.local_fallback(
                            item.agent_event, status="hard_deadline_fallback"
                        )
                        item.timing_ready.set()
                        if item.classification_task is not None:
                            item.classification_task.cancel()
                frozen_action = batch.action()
                if frozen_action == "ignore":
                    return "ignore", True, max(0.0, time.monotonic() - batch.created_at)
                return "reply_now", True, max(0.0, time.monotonic() - batch.created_at)
            if batch.phase == "cancelled" or self._closing:
                return "cancelled", False, max(0.0, time.monotonic() - batch.created_at)

            action = batch.action()
            if action in {"ignore", "reply_now"}:
                return action, False, max(0.0, time.monotonic() - batch.created_at)
            if any(not item.merge_safe for item in batch.items):
                return "reply_now", False, max(0.0, time.monotonic() - batch.created_at)
            now = time.monotonic()
            if now >= hard_deadline:
                return "reply_now", True, max(0.0, now - batch.created_at)
            quiet_deadline = batch.last_input_at + self._config.response_wait_quiet_seconds
            target = min(quiet_deadline, hard_deadline)
            if now >= target:
                return "reply_now", target == hard_deadline, max(0.0, now - batch.created_at)
            batch.changed.clear()
            try:
                await asyncio.wait_for(batch.changed.wait(), timeout=target - now)
            except asyncio.TimeoutError:
                continue
        return "cancelled", False, max(0.0, time.monotonic() - batch.created_at)

    @staticmethod
    def _batch_timing_diagnostics(
        batch: PendingReplyBatch, actual_wait_seconds: float
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item in batch.items:
            timing = item.timing
            if timing is None:
                diagnostic: dict[str, object] = {"status": "pending"}
            else:
                diagnostic = dict(timing.diagnostic)
                diagnostic.update({
                    "selected_action": timing.action,
                    "selected_source": timing.source,
                    "confidence": timing.confidence,
                })
            diagnostic["event_id"] = item.agent_event.event_id
            diagnostic["actual_wait_seconds"] = round(actual_wait_seconds, 3)
            result.append(diagnostic)
        return result

    def _make_batch_event(
        self,
        batch: PendingReplyBatch,
        *,
        actual_wait_seconds: float,
        hard_deadline_reached: bool,
    ) -> AgentEvent:
        items = list(batch.items)
        first = items[0].agent_event
        last = items[-1].agent_event
        texts = [item.agent_event.text or "" for item in items]
        joined_text = "\n".join(texts)
        messages = [
            {
                "event_id": item.agent_event.event_id,
                "timestamp": item.agent_event.timestamp,
                "text": item.agent_event.text or "",
            }
            for item in items
        ]
        timing_diagnostics = self._batch_timing_diagnostics(batch, actual_wait_seconds)
        metadata = dict(first.metadata)
        metadata.update({
            "merge_safe": all(item.merge_safe for item in items),
            "timing_force_reply": True,
            "response_timing": {
                "decisions": timing_diagnostics,
                "actual_wait_seconds": round(actual_wait_seconds, 3),
                "hard_deadline_reached": hard_deadline_reached,
                "message_count": len(items),
            },
        })
        payload = dict(first.payload)
        payload["text"] = joined_text
        payload["user_messages"] = messages
        return AgentEvent(
            event_id=first.event_id,
            trace_id=first.trace_id,
            type=first.type,
            source=first.source,
            session_id=first.session_id,
            user_id=first.user_id,
            timestamp=last.timestamp,
            text=joined_text,
            metadata=metadata,
            event_type=first.event_type,
            payload=payload,
        )

    def _is_batch_current(
        self, batch: PendingReplyBatch, version: int, epoch: int
    ) -> bool:
        return (
            not self._closing
            and (getattr(self, "_data_service", None) is None or all(self._data_service.is_current(item.agent_event) for item in batch.items))
            and batch.phase != "cancelled"
            and batch.version == version
            and self._reactive_epoch[batch.session_id] == epoch
        )

    async def _handle_without_timing(self, framework_event: AstrMessageEvent):
        """Compatibility path: no merge window, Jev timing, or stale-send cutoff."""
        task = asyncio.current_task()
        if task is not None:
            self._reactive_tasks.add(task)
        try:
            agent_event = self._message_adapter.to_agent_event(framework_event)
            if getattr(self, "_data_service", None) is not None:
                self._data_service.stamp(agent_event)
            agent_event.metadata["_turn_deadline_monotonic"] = time.monotonic() + self._config.turn_timeout
            if not await self._agent_loop.on_user_event(agent_event):
                framework_event.stop_event()
                return
            item = PendingMessage(
                agent_event=agent_event,
                framework_event=framework_event,
                merge_safe=False,
                revision=0,
                timing=ReplyTimingDecision(
                    "reply_now", "disabled", 1.0,
                    {"status": "timing_disabled", "selected_action": "reply_now"},
                ),
            )
            item.timing_ready.set()
            batch = PendingReplyBatch(
                session_id=agent_event.session_id,
                items=[item],
                phase="processing",
                max_messages=1,
            )
            version = batch.version
            epoch = self._reactive_epoch[agent_event.session_id]
            async with self._guard_user_data(agent_event.user_id), self._send_coordinator.lock_for(agent_event.session_id):
                async def always_current() -> bool:
                    return not self._closing

                response = await self._agent.handle_event(
                    agent_event, is_current=always_current
                )
                if not response.text:
                    delivery: dict[str, object] = {
                        "status": "no_text",
                        "segments_yielded": 0,
                        "yielded_text_segments": [],
                        "audio_yielded": False,
                        "emoji_send_completed": False,
                        "delivery_confirmation": "platform_acceptance_unknown",
                    }
                    await self._agent.commit_delivery(agent_event, response, "")
                    await self._record_test_turn(agent_event, response, delivery)
                    framework_event.stop_event()
                    return
                delivery: dict[str, object] = {
                    "status": "not_sent",
                    "segments_yielded": 0,
                    "yielded_text_segments": [],
                    "audio_yielded": False,
                    "emoji_send_completed": False,
                    "delivery_confirmation": "platform_acceptance_unknown",
                }
                outcome = {"stale_before_output": False, "stopped_for_new_input": False}
                async with aclosing(self._deliver_response_locked(
                    framework_event, agent_event, response, batch,
                    version, epoch, delivery, outcome,
                )) as stream:
                    async for result in stream:
                        yield result
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Alice Agent adapter failed while processing a private message")
            yield framework_event.plain_result(self._config.fallback_text)
            framework_event.stop_event()
        finally:
            if task is not None:
                self._reactive_tasks.discard(task)

    async def _run_pending_batch(self, batch: PendingReplyBatch):
        owner_event = batch.items[0].framework_event
        session_id = batch.session_id
        try:
            while not self._closing:
                service = getattr(self, "_data_service", None)
                if service is not None and any(not service.is_current(item.agent_event) for item in batch.items):
                    batch.phase = "cancelled"
                    owner_event.stop_event()
                    return
                action, hard_deadline_reached, actual_wait = await self._resolve_batch_timing(batch)
                if action == "cancelled" or self._closing:
                    return
                snapshot_version = batch.version
                snapshot_epoch = self._reactive_epoch[session_id]
                agent_event = self._make_batch_event(
                    batch,
                    actual_wait_seconds=actual_wait,
                    hard_deadline_reached=hard_deadline_reached,
                )
                agent_event.metadata["_turn_deadline_monotonic"] = batch.created_at + self._config.turn_timeout
                if not await self._pending_replies.wait_turn(batch):
                    return
                if not self._is_batch_current(batch, snapshot_version, snapshot_epoch):
                    continue

                delivery: dict[str, object] = {
                    "status": "not_sent",
                    "segments_yielded": 0,
                    "yielded_text_segments": [],
                    "audio_yielded": False,
                    "emoji_send_completed": False,
                    "delivery_confirmation": "platform_acceptance_unknown",
                    "timing_wait_seconds": round(actual_wait, 3),
                }
                outcome = {"stale_before_output": False, "stopped_for_new_input": False}
                if not self._is_batch_current(batch, snapshot_version, snapshot_epoch):
                    continue
                if action == "ignore":
                    batch.phase = "delivering"
                    response = AgentResponse(
                        text=None,
                        trace_id=agent_event.trace_id,
                        action="ignore",
                        metadata={
                            "turn_diagnostics": {
                                "response_timing": agent_event.metadata.get("response_timing", {})
                            }
                        },
                    )
                    delivery["status"] = "ignored_by_timing"
                    await self._agent.record_suppressed(
                        agent_event,
                        agent_event.metadata.get("response_timing", {}),
                    )
                    await self._record_test_turn(agent_event, response, delivery)
                    owner_event.stop_event()
                    return

                # Generation is intentionally outside the shared send lock:
                # intake can invalidate this revision immediately, and
                # proactive sends are not held behind a potentially slow LLM.
                batch.phase = "processing"

                async def guard() -> bool:
                    return self._is_batch_current(batch, snapshot_version, snapshot_epoch)

                try:
                    response = await self._agent.handle_event(
                        agent_event, is_current=guard
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.exception("Alice Agent pipeline failed while handling a private message")
                    response = AgentResponse(
                        text=self._config.fallback_text,
                        trace_id=agent_event.trace_id,
                        metadata={
                            "fallback": True,
                            "turn_diagnostics": {
                                "pipeline_failure": {"error_kind": type(exc).__name__}
                            },
                        },
                    )
                if response.action == "superseded" or not self._is_batch_current(
                    batch, snapshot_version, snapshot_epoch
                ):
                    outcome["stale_before_output"] = True
                else:
                    async with self._guard_user_data(agent_event.user_id), self._send_coordinator.lock_for(session_id):
                        if not self._is_batch_current(batch, snapshot_version, snapshot_epoch):
                            outcome["stale_before_output"] = True
                        else:
                            async with aclosing(self._deliver_response_locked(
                                owner_event,
                                agent_event,
                                response,
                                batch,
                                snapshot_version,
                                snapshot_epoch,
                                delivery,
                                outcome,
                            )) as stream:
                                async for result in stream:
                                    yield result

                if outcome["stale_before_output"]:
                    await self._await_pending_registrations(session_id)
                    continue
                owner_event.stop_event()
                return
        except asyncio.CancelledError:
            if batch.phase not in {"done", "cancelled", "delivering"} and not self._closing:
                try:
                    event = self._make_batch_event(
                        batch,
                        actual_wait_seconds=max(0.0, time.monotonic() - batch.created_at),
                        hard_deadline_reached=False,
                    )
                    await self._agent.record_suppressed(
                        event, event.metadata.get("response_timing", {})
                    )
                except Exception:
                    self.logger.exception("Alice Agent could not preserve cancelled input safely")
            raise
        finally:
            await self._pending_replies.finish(batch)
