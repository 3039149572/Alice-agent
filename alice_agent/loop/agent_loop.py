from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime
from uuid import uuid4

from ..config import AgentConfig
from ..core.session_manager import SessionManager
from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.perception import PerceptionResult
from ..drives.manager import DriveManager
from ..events.bus import EventBus
from ..events.models import new_event
from ..events.types import EventType
from ..memory.manager import MemoryManager
from ..ports.logging import LoggerPort
from ..proactive.cooldown import CooldownManager
from ..proactive.daily_policy import DailyProactivePolicy
from ..proactive.attempts import select_daily_event
from ..proactive.unanswered import unanswered_status
from ..async_utils import finish_before_cancelling
from ..proactive.evaluator import ProactiveEvaluator
from ..proactive.models import (
    ActionStatus,
    CandidateActionType,
    LoopMetrics,
    ProactiveActionPlan,
    ScheduledContextEvent,
    UserAvailability,
)
from ..proactive.policies import active_conversation
from ..proactive.scorer import ActionScorer
from ..relationship.manager import RelationshipManager
from ..state.manager import StateManager


_MAX_PROACTIVE_INACTIVITY_SECONDS = 7 * 24 * 60 * 60


class AgentLoop:
    """Cheap event-driven loop. LLM work only occurs after every rule gate passes."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        event_bus: EventBus,
        clock: object,
        store: object,
        scheduler: object,
        drive_manager: DriveManager,
        state_manager: StateManager,
        daily_events: object | None = None,
        relationship_manager: RelationshipManager,
        memory: MemoryManager,
        proactive_planner: object,
        response_generator: object,
        scorer: ActionScorer,
        evaluator: ProactiveEvaluator,
        cooldown: CooldownManager,
        sender: object,
        session_manager: SessionManager,
        logger: LoggerPort,
        decision_gate: object | None = None,
        daily_policy: object | None = None,
        data_service: object | None = None,
        delivery_coordinator: object | None = None,
        audit: object | None = None,
    ) -> None:
        self._config = config
        self._bus = event_bus
        self._clock = clock
        self._store = store
        self._scheduler = scheduler
        self._drives = drive_manager
        self._states = state_manager
        self._daily_events = daily_events
        self._data_service = data_service
        self._delivery = None
        if delivery_coordinator is not None:
            from ..core.proactive_delivery import ProactiveDeliveryCommitter
            self._delivery = ProactiveDeliveryCommitter(delivery_coordinator.journal, store, memory, session_manager)
            delivery_coordinator.proactive_handler = self._delivery.apply
        self._relationships = relationship_manager
        self._memory = memory
        self._planner = proactive_planner
        self._responses = response_generator
        self._scorer = scorer
        self._evaluator = evaluator
        self._cooldown = cooldown
        self._sender = sender
        self._sessions = session_manager
        self._logger = logger
        self._audit_sink = audit
        self._audit_context = ContextVar("alice_proactive_audit", default=None)
        self._attempt_context = ContextVar("alice_proactive_attempt", default=None)
        self._decision_gate = decision_gate
        self._daily_policy = daily_policy or DailyProactivePolicy(
            store, base_probability=config.proactive_daily_probability,
        )
        self._diagnostics: dict[tuple[str, str], float] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.metrics = LoopMetrics()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def inspect_session(self, session_id: str, user_id: str) -> dict:
        from ..proactive.diagnostics import ProactiveInspector
        return await ProactiveInspector(self).inspect(session_id, user_id)

    async def _audit(self, stage: str, **details) -> None:
        context = self._audit_context.get()
        if context is not None:
            context["last_stage"] = stage
            details = {**{k: v for k, v in context.items() if k != "last_stage"}, **details}
        if self._audit_sink is not None:
            try:
                await self._audit_sink.record(stage, **details)
            except Exception:
                self._logger.warning("Alice proactive diagnostic sink failed safely")

    async def start(self) -> None:
        if not self._config.agent_loop_enabled:
            await self._audit("loop_disabled")
        if not self._config.agent_loop_enabled or self.running:
            return
        self._stopping = False
        await self._bus.publish(new_event(
            EventType.SYSTEM_START, source="alice_loop", timestamp=self._clock.now()
        ))
        self._task = asyncio.create_task(self._run(), name="alice-agent-loop")
        self._logger.info("Alice Agent Loop started")
        await self._audit("loop_started")
        self._logger.info(
            f"PROACTIVE startup enabled={self._config.proactive_enabled} "
            f"daily_events={self._config.daily_events_enabled} "
            f"events_max={self._config.daily_event_max} "
            f"window={self._config.proactive_window_start}-{self._config.proactive_window_end} "
            f"local_time={datetime.fromtimestamp(self._clock.now()).isoformat()} "
            f"timezone={datetime.now().astimezone().tzinfo} "
            f"daily_threshold={self._config.proactive_daily_score_threshold} "
            f"budget_24h={self._config.proactive_daily_budget}"
        )

    def _diagnose(self, session_id: str, reason: str, now: float) -> None:
        key = (session_id, reason)
        previous = self._diagnostics.get(key)
        if previous is None or now - previous >= 3600:
            self._logger.info(f"PROACTIVE session={session_id} decision={reason}")
            self._diagnostics[key] = now
            if len(self._diagnostics) > 2048:
                self._diagnostics = {k: t for k, t in self._diagnostics.items() if now - t < 3600}

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        await self._bus.publish(new_event(
            EventType.SYSTEM_SHUTDOWN, source="alice_loop", timestamp=self._clock.now()
        ))
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._logger.info("Alice Agent Loop stopped")

    async def _run(self) -> None:
        consecutive_errors = 0
        while not self._stopping:
            try:
                await self.tick()
                consecutive_errors = 0
                delay = self._config.agent_tick_interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                await self._audit("loop_error", error_type=type(exc).__name__, consecutive_errors=consecutive_errors)
                self._logger.exception(
                    f"Alice Agent Loop tick failed consecutive={consecutive_errors}"
                )
                delay = min(
                    300.0,
                    self._config.agent_tick_interval * (2 ** min(consecutive_errors, 4)),
                )
            await self._clock.sleep(delay)

    async def on_user_event(self, event: AgentEvent) -> bool:
        revision = await self.register_user_event(event)
        if revision is None:
            return False
        await self.apply_user_activity(event)
        return True

    async def register_user_event(self, event: AgentEvent) -> int | None:
        """Invalidate old proactive work without waiting for the session lock."""
        if not await self._bus.publish(event):
            self._logger.debug(f"Duplicate AgentEvent ignored id={event.event_id}")
            return None
        revision = await self._store.upsert_session(
            event.session_id, event.user_id, event.timestamp
        )
        await self._store.cancel_pending(event.session_id)
        return int(revision)

    async def apply_user_activity(self, event: AgentEvent) -> None:
        """Apply drive changes after the caller has registered the event revision."""
        async with self._sessions.lock(event.session_id):
            await self._drives.on_user_message(
                event.session_id, event.user_id, event.timestamp
            )

    async def schedule(self, event: ScheduledContextEvent) -> None:
        await self._scheduler.schedule(event)

    async def tick(self) -> None:
        now = self._clock.now()
        self.metrics.total_ticks += 1
        await self._scheduler.maintenance(now)
        due = await self._scheduler.due(now)
        due_by_session: dict[str, ScheduledContextEvent] = {}
        for scheduled in due:
            due_by_session.setdefault(scheduled.session_id, scheduled)
        sessions = await self._store.list_sessions()
        await self._audit("tick", session_count=len(sessions), scheduled_count=len(due))
        for session in sessions:
            await self._bus.publish(new_event(
                EventType.TIME_TICK,
                source="alice_loop",
                timestamp=now,
                session_id=str(session["session_id"]),
                user_id=str(session["user_id"]),
                payload={"last_tick_at": float(session["last_tick_at"])},
            ))
        for event in self._bus.drain_nowait():
            if event.event_type == EventType.TIME_TICK.value:
                await self._evaluate_session(event, due_by_session.get(event.session_id))

    async def _do_nothing(self, reason: str, *, score: float | None = None,
                          session_id: str = "", reason_code: str | None = None, **details) -> None:
        self.metrics.do_nothing_count += 1
        suffix = "" if score is None else f" score={score:.3f}"
        self._logger.debug(f"PROACTIVE decision=DO_NOTHING reason={reason}{suffix}")
        await self._audit("blocked", session_id=session_id, reason=reason, reason_code=reason_code or reason,
                          score=score, **details)
        self._diagnose(session_id, reason, self._clock.now())

    def _retry_options(self):
        return {"retry_base": self._config.proactive_retry_base_seconds,
                "retry_max": self._config.proactive_retry_max_seconds}

    def _daily_window_open(self, day):
        now = self._clock.now()
        return self._evaluator.in_send_window(now) and datetime.fromtimestamp(now).date().isoformat() == day

    async def _finish_attempt(self, *, retryable=False, error_kind=None):
        claim = self._attempt_context.get()
        if claim is None:
            return True
        now = self._clock.now()
        if claim["kind"] == "daily":
            result = await self._store.finish_daily_proactive_attempt(
                claim["session_id"], claim["day"], claim["event_id"], claim["claim_id"],
                now=now, retryable=retryable, error_kind=error_kind, **self._retry_options())
        else:
            current = next((item for item in await self._scheduler.due(now) if item.id == claim["event_id"]), None)
            attempt = current.metadata.get("proactive_retry", {}) if current else {}
            result = {"changed": False}
            if attempt.get("claim_id") == claim["claim_id"] and attempt.get("status") == "pending":
                attempt.update(status="retry" if retryable else "finished", lease_until=0.0)
                if retryable:
                    attempt["failure_count"] = int(attempt.get("failure_count", 0)) + 1
                    delay = min(self._config.proactive_retry_max_seconds,
                                self._config.proactive_retry_base_seconds * 2 ** min(attempt["failure_count"] - 1, 20))
                    attempt.update(next_retry_at=now + delay, error_kind=error_kind)
                await self._scheduler.schedule(current)
                result = {"changed": True, **attempt}
        self._attempt_context.set(None)
        if result.get("changed"):
            actual_retry = result.get("status") == "retry"
            await self._audit("retry_scheduled" if actual_retry else "attempt_finalized",
                              source=claim["kind"], event_id=claim["event_id"],
                              error_type=error_kind, next_retry_at=result.get("next_retry_at"),
                              retry_in_seconds=max(0, result.get("next_retry_at", now) - now) if actual_retry else None,
                              failure_count=result.get("failure_count", 0), quota_refunded=False)
        return bool(result.get("changed"))

    async def _evaluate_session(
        self, tick_event: AgentEvent, scheduled: ScheduledContextEvent | None
    ) -> None:
        token = self._audit_context.set({"session_id": tick_event.session_id,
                                         "user_id": tick_event.user_id, "tick_id": tick_event.event_id})
        attempt_token = self._attempt_context.set(None)
        abort_kind = "EvaluationAborted"
        try:
            await self._audit("evaluation", scheduled=scheduled is not None)
            await self._evaluate_session_guarded(tick_event, scheduled)
        except asyncio.CancelledError:
            abort_kind = "CancelledError"
            await self._audit("cancelled", interrupted_stage=self._audit_context.get().get("last_stage"))
            raise
        except Exception as exc:
            abort_kind = type(exc).__name__
            await self._audit("evaluation_error", error_type=type(exc).__name__,
                              interrupted_stage=self._audit_context.get().get("last_stage"))
            raise
        finally:
            try:
                try:
                    if self._attempt_context.get() is not None:
                        await finish_before_cancelling(self._finish_attempt(retryable=True, error_kind=abort_kind))
                except Exception:
                    self._logger.exception("Alice proactive attempt cleanup will recover after its lease expires")
            finally:
                self._attempt_context.reset(attempt_token)
                self._audit_context.reset(token)

    async def _evaluate_session_guarded(self, tick_event, scheduled) -> None:
        if self._data_service is None:
            await self._evaluate_session_with_data(tick_event, scheduled)
            return
        async with self._data_service.guard(tick_event.user_id):
            if scheduled is not None:
                live = await self._scheduler.due(tick_event.timestamp)
                scheduled = next((item for item in live if item.id == scheduled.id), None)
            await self._evaluate_session_with_data(tick_event, scheduled)

    async def _evaluate_session_with_data(self, tick_event, scheduled) -> None:
        now = tick_event.timestamp
        session_id = tick_event.session_id
        user_id = tick_event.user_id
        last_tick = float(tick_event.payload.get("last_tick_at", now))
        elapsed = max(0.0, now - last_tick)
        rows = await self._store.list_sessions()
        row = next((item for item in rows if item["session_id"] == session_id), {})
        revision = int(row.get("revision", 0))
        last_interaction = (
            float(row["last_interaction_at"])
            if row.get("last_interaction_at") is not None else None
        )
        recently_active = (
            last_interaction is not None
            and max(0.0, now - last_interaction) <= _MAX_PROACTIVE_INACTIVITY_SECONDS
        )
        availability = UserAvailability(
            last_interaction_time=last_interaction,
            known_active_session=True,
        )
        unresolved_count = await self._scheduler.unresolved_count(session_id, now)
        daily_events: list[dict[str, object]] = []
        async with self._sessions.lock(session_id):
            history = await self._sessions.load_history(session_id)
            previous_drives = await self._drives.get(session_id, user_id)
            state = await self._states.advance_time(
                session_id, elapsed,
                inactivity_seconds=(
                    max(0.0, now - last_interaction) if last_interaction is not None else None
                ),
                rest_need=previous_drives.rest_need if self._config.drive_rest_enabled else 0.0,
            )
            relationship = await self._relationships.get(user_id)
            drives = await self._drives.advance(
                session_id, user_id, now, state, relationship,
                unresolved_context_count=unresolved_count,
            )
            await self._store.update_tick(session_id, now)

        self._logger.debug(
            f"DRIVES session={session_id} social={drives.social_need:.3f} "
            f"curiosity={drives.curiosity:.3f} rest={drives.rest_need:.3f}"
        )
        if active_conversation(availability, now, self._config.active_conversation_window):
            await self._do_nothing("active conversation window", session_id=session_id)
            return
        in_send_window = self._evaluator.in_send_window(now)
        if not self._config.proactive_enabled:
            await self._do_nothing("proactive disabled", session_id=session_id)
            return
        if not scheduled and not in_send_window:
            await self._do_nothing("outside local send window", session_id=session_id)
            return
        if scheduled is None:
            contact = await unanswered_status(self._store, self._config, session_id, last_interaction, now)
            await self._audit("unanswered", check="evaluation", **contact)
            if not contact["allowed"]:
                await self._do_nothing(contact["reason_code"], session_id=session_id,
                                       next_evaluable_at=contact["next_evaluable_at"],
                                       recent_unanswered_count=contact["recent_unanswered_count"])
                return

        if scheduled:
            retry = scheduled.metadata.get("proactive_retry", {})
            if retry.get("status") == "pending" and float(retry.get("lease_until", 0)) <= now:
                self._attempt_context.set({"kind": "scheduled", "event_id": scheduled.id,
                                           "claim_id": retry["claim_id"]})
                await self._finish_attempt(retryable=True, error_kind="AttemptLeaseExpired")
                scheduled = next((item for item in await self._scheduler.due(now) if item.id == scheduled.id), scheduled)
                retry = scheduled.metadata.get("proactive_retry", {})
            if int(retry.get("attempt_count", 0)) >= 3:
                await self._scheduler.mark_evaluated(scheduled.id)
                await self._do_nothing("scheduled_retry_limit", session_id=session_id)
                return
            wait_until = float(retry.get("lease_until" if retry.get("status") == "pending" else "next_retry_at", 0))
            if wait_until > now:
                await self._do_nothing("retry_wait", session_id=session_id, next_retry_at=wait_until,
                                       retry_in_seconds=wait_until - now, source="scheduled")
                return
        elif self._daily_events is not None and recently_active and in_send_window:
            daily_diagnostic = {}
            await self._audit("daily_events_start", enabled=self._config.daily_events_enabled,
                              event_limit=self._config.daily_event_max)
            async with self._sessions.lock(session_id):
                try:
                    events = await self._daily_events.ensure(session_id, user_id, now, history,
                                                             diagnostics=daily_diagnostic)
                    if isinstance(events, list):
                        daily_events = [item for item in events if isinstance(item, dict)]
                    state = await self._states.get(session_id)
                    await self._audit("daily_events", event_count=len(daily_events), **daily_diagnostic)
                except Exception as exc:
                    await self._audit("daily_events_error", error_type=type(exc).__name__, reason_code="daily_event_error")
                    await self._do_nothing("daily_event_error", session_id=session_id)
                    self._logger.exception("Alice daily event tick failed safely")
                    return

        daily_event: dict[str, object] | None = None
        no_candidate_reason = None
        if not scheduled and not self._daily_window_open(datetime.fromtimestamp(now).date().isoformat()):
            await self._do_nothing("daily_window_elapsed", session_id=session_id)
            return
        if daily_events and recently_active and in_send_window and not scheduled:
            local_day = datetime.fromtimestamp(now).date().isoformat()
            cached = await self._store.get_daily_proactive_decision(session_id, local_day)
            if cached is not None and not cached["eligible"]:
                await self._audit("probability", cached=True, **{k: cached.get(k) for k in
                                  ("eligible", "probability", "roll", "source", "reason")})
                await self._do_nothing("daily probability chose silence", session_id=session_id)
                return
            daily_state = await self._store.get_daily_proactive_state(session_id, local_day)
            if any(item.get("status") == "pending" and float(item.get("lease_until", 0)) <= now
                   for item in daily_state.get("event_attempts", {}).values()):
                recovered = await self._store.recover_daily_proactive_attempts(session_id, local_day, now, **self._retry_options())
                for item in recovered:
                    await self._audit("attempt_recovered", **item)
                daily_state = await self._store.get_daily_proactive_state(session_id, local_day)
            attempts = int(daily_state.get("attempt_count", 0))
            attempt_limit = max(0, int(getattr(self._config, "proactive_daily_attempt_limit", 2)))
            attempted = set(daily_state.get("attempted_event_ids", []))
            delivered = set(daily_state.get("delivered_event_ids", []))
            await self._audit("attempt", attempt_count=attempts, attempt_limit=attempt_limit,
                              attempted_event_count=len(attempted), delivered_event_count=len(delivered),
                              limit_period="local_calendar_day", event_attempts=daily_state.get("event_attempts", {}))
            daily_event, no_candidate_reason, retry_at = select_daily_event(daily_events, daily_state, now, attempt_limit)
            if no_candidate_reason in {"retry_wait", "attempt_in_progress"}:
                await self._do_nothing(no_candidate_reason, session_id=session_id, next_retry_at=retry_at,
                                       retry_in_seconds=max(0, retry_at - now))
                return

        if not scheduled and not daily_event:
            no_candidate_reason = no_candidate_reason or (
                "session_inactive" if not recently_active else
                "daily_events_disabled" if not self._config.daily_events_enabled else
                "daily_events_empty"
            )
            await self._audit("candidate_availability", reason=no_candidate_reason, reason_code=no_candidate_reason)
            self._diagnose(session_id, no_candidate_reason, now)

        candidate = self._evaluator.evaluate(
            drives, availability, now, scheduled, daily_event=daily_event
        )
        await self._audit("candidate", decision=candidate.action_type.value,
                          source="scheduled" if scheduled else "daily_event" if daily_event else "unfinished_context",
                          action_id=candidate.action_id, reason=candidate.reason)
        if candidate.action_type == CandidateActionType.DO_NOTHING:
            await self._do_nothing(no_candidate_reason or candidate.reason, session_id=session_id)
            return
        self.metrics.candidate_actions += 1
        candidate.metadata["session_id"] = session_id
        await self._store.save_action(candidate)

        if candidate.expires_at is not None and now > candidate.expires_at:
            candidate.status = ActionStatus.EXPIRED
            await self._store.save_action(candidate)
            await self._do_nothing("candidate expired", session_id=session_id)
            return
        allowed, cooldown_reason = await self._cooldown.check(
            user_id, candidate.topic, now, session_id=session_id
        )
        await self._audit("cooldown", allowed=allowed, reason=cooldown_reason)
        if not allowed:
            if cooldown_reason == "daily_budget":
                self.metrics.blocked_by_daily_budget += 1
            else:
                self.metrics.blocked_by_cooldown += 1
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            if scheduled:
                await self._scheduler.mark_evaluated(scheduled.id)
            await self._do_nothing(cooldown_reason, session_id=session_id)
            return
        is_daily_event = bool(candidate.metadata.get("daily_event_id"))
        daily_threshold = self._config.proactive_daily_score_threshold
        decision = self._scorer.score(
            candidate, drives, relationship,
            threshold_override=daily_threshold if is_daily_event else None,
        )
        await self._audit("score", score=decision.score, decision=decision.decision,
                          threshold=daily_threshold if is_daily_event else self._config.proactive_score_threshold,
                          reason=decision.reason)
        self._logger.debug(
            f"PROACTIVE action_id={candidate.action_id} score={decision.score:.3f} "
            f"threshold={daily_threshold if is_daily_event else self._config.proactive_score_threshold:.3f} "
            f"decision={decision.decision}"
        )
        if decision.decision != "allow" or not self._config.proactive_enabled:
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            if scheduled:
                await self._scheduler.mark_evaluated(scheduled.id)
            await self._do_nothing(decision.reason, score=decision.score, session_id=session_id)
            return

        daily_event_id = str(candidate.metadata.get("daily_event_id", ""))
        daily_event_day = str(candidate.metadata.get("daily_event_day", ""))
        lease_until = self._clock.now() + max(300.0, self._config.planner_timeout * 2
                                             + self._config.llm_timeout * 2 + self._config.jev_timeout_ms / 1000 + 120)
        if daily_event_id and daily_event_day:
            if not self._daily_window_open(daily_event_day):
                await self._do_nothing("daily_window_elapsed", session_id=session_id)
                return
            await self._audit("probability_start")
            daily_decision = await self._daily_policy.evaluate(
                session_id, daily_event_day, state, daily_events, now,
                before_model=lambda: self._daily_window_open(daily_event_day),
            )
            await self._audit("probability", **{k: daily_decision.get(k) for k in
                              ("eligible", "probability", "roll", "source", "reason", "deferred",
                               "model_status", "error_kind", "failure_reason", "http_status")}, cached=cached is not None)
            if daily_decision.get("deferred"):
                await self._do_nothing("daily_window_elapsed", session_id=session_id)
                return
            self._diagnose(session_id, (
                f"daily_probability={daily_decision['probability']:.3f} "
                f"roll={daily_decision['roll']:.3f} eligible={daily_decision['eligible']} "
                f"source={daily_decision['source']}"
            ), now)
            if not daily_decision["eligible"]:
                candidate.status = ActionStatus.CANCELLED
                await self._store.save_action(candidate)
                await self._do_nothing("daily probability chose silence", session_id=session_id)
                return
            if not self._daily_window_open(daily_event_day):
                await self._do_nothing("daily_window_elapsed", session_id=session_id)
                return
            candidate.metadata["daily_decision"] = daily_decision
            attempt_limit = max(0, int(getattr(self._config, "proactive_daily_attempt_limit", 2)))
            claimed = await self._store.claim_daily_proactive_attempt(
                session_id, daily_event_day, daily_event_id, attempt_limit,
                now=self._clock.now(), claim_id=candidate.action_id, lease_until=lease_until,
            )
            if not claimed:
                candidate.status = ActionStatus.CANCELLED
                await self._store.save_action(candidate)
                await self._do_nothing("attempt_claim_conflict", session_id=session_id)
                return
            self._attempt_context.set({"kind": "daily", "session_id": session_id, "day": daily_event_day,
                                       "event_id": daily_event_id, "claim_id": candidate.action_id})
            await self._audit("attempt_claimed", event_id=daily_event_id, claim_id=candidate.action_id,
                              lease_until=lease_until, quota_refunded=False)
        elif scheduled:
            previous = scheduled.metadata.get("proactive_retry", {})
            scheduled.metadata["proactive_retry"] = {
                "status": "pending", "claim_id": candidate.action_id, "lease_until": lease_until,
                "attempt_count": int(previous.get("attempt_count", 0)) + 1,
                "failure_count": int(previous.get("failure_count", 0)), "next_retry_at": 0.0}
            await self._scheduler.schedule(scheduled)
            self._attempt_context.set({"kind": "scheduled", "event_id": scheduled.id, "claim_id": candidate.action_id})

        if revision != await self._store.get_revision(session_id):
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("new user event invalidated daily evaluation", session_id=session_id)
            return
        perception = PerceptionResult(
            intent="follow_up", topic=candidate.topic or "follow-up",
            importance=0.65, memory_candidate=False, confidence=1.0,
        )
        agent_event = AgentEvent(
            event_id=f"evt_{uuid4().hex}", trace_id=f"alice_proactive_{uuid4().hex[:12]}",
            type=EventType.TIME_TICK.value, event_type=EventType.TIME_TICK.value,
            source="alice_loop", session_id=session_id, user_id=user_id,
            timestamp=now, text=str(candidate.metadata.get("summary", candidate.reason)),
            metadata={"proactive": True, "action_id": candidate.action_id},
            payload={},
        )
        self._audit_context.get()["trace_id"] = agent_event.trace_id
        await self._audit("memory_start")
        try:
            memories = await self._memory.recall(agent_event, perception, state, relationship)
        except Exception as exc:
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._finish_attempt(retryable=True, error_kind=type(exc).__name__)
            await self._do_nothing("memory recall failed", session_id=session_id, error_type=type(exc).__name__)
            return
        await self._audit("memory", memory_count=len(memories))

        if daily_event_day and not self._daily_window_open(daily_event_day):
            await self._do_nothing("daily_window_elapsed", session_id=session_id)
            return

        gate_decision = None
        if self._decision_gate is not None:
            await self._audit("jev_start")
            gate_decision = await self._decision_gate.evaluate(
                candidate, drives, relationship, memories, availability, agent_event.trace_id
            )
            await self._audit("jev", decision=gate_decision.action, shadow_action=gate_decision.shadow_action,
                              reason=gate_decision.reason, retryable=getattr(gate_decision, "retryable", False),
                              **getattr(gate_decision, "diagnostic", {}))
            if gate_decision.action in {"do_nothing", "defer"}:
                retryable = bool(getattr(gate_decision, "retryable", False))
                await self._finish_attempt(retryable=retryable,
                                           error_kind=getattr(gate_decision, "diagnostic", {}).get("error_kind", "DecisionUnavailable") if retryable else None)
                candidate.status = ActionStatus.CANCELLED
                await self._store.save_action(candidate)
                if scheduled and not retryable:
                    await self._scheduler.mark_evaluated(scheduled.id)
                await self._do_nothing(gate_decision.reason, score=decision.score, session_id=session_id,
                                       reason_code="jev_unavailable" if retryable else "jev_decision_rejected",
                                       retryable=retryable)
                return

        if daily_event_day and not self._daily_window_open(daily_event_day):
            await self._do_nothing("daily_window_elapsed", session_id=session_id)
            return
        if not await self._finish_attempt():
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("attempt_ownership_lost", session_id=session_id)
            return
        candidate.status = ActionStatus.PLANNED
        await self._store.save_action(candidate)
        if gate_decision is not None and gate_decision.action == "send":
            plan = ProactiveActionPlan(
                action="proactive_message",
                reason=gate_decision.reason,
                topic=candidate.topic,
                response_goal=candidate.reason,
                memory_ids=[item.id for item in memories[:1]],
            )
        else:
            self.metrics.proactive_planner_calls += 1
            await self._audit("planner_start")
            plan = await self._planner.plan(
                session_id, candidate, state, relationship, drives, memories, availability
            )
            if gate_decision is not None and gate_decision.shadow_action is not None:
                existing_action = "send" if plan.action == "proactive_message" else "do_nothing"
                self._decision_gate.record_shadow_comparison(
                    agent_event.trace_id, gate_decision.shadow_action, existing_action
                )
        await self._audit("planner", decision=plan.action, reason=plan.reason,
                          source="jev" if gate_decision is not None and gate_decision.action == "send" else "planner")
        if plan.action != "proactive_message":
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            if scheduled:
                await self._scheduler.mark_evaluated(scheduled.id)
            await self._do_nothing(plan.reason or "planner chose silence", score=decision.score, session_id=session_id)
            return
        if revision != await self._store.get_revision(session_id):
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("new user event invalidated action", session_id=session_id)
            return

        selected = self._memory.select_for_response(memories, plan.memory_ids)
        if daily_event_day and not self._daily_window_open(daily_event_day):
            await self._do_nothing("daily_window_elapsed", session_id=session_id)
            return
        action_plan = ActionPlan(
            action="recall_then_reply" if selected else "reply",
            intent="proactive_follow_up", response_goal=plan.response_goal or candidate.reason,
            memory_ids=[item.id for item in selected], source="proactive",
        )
        history = await self._sessions.load_history(session_id)
        await self._audit("generation_start")
        try:
            response = await self._responses.generate(
                agent_event, perception, state, selected, action_plan,
                history, relationship,
            )
        except Exception:
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("response generation failed safely", session_id=session_id)
            return
        await self._audit("generation", generated_chars=len(response.text or ""))
        if revision != await self._store.get_revision(session_id):
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("new user event invalidated generated action", session_id=session_id)
            return
        if not response.text:
            await self._do_nothing("empty proactive response", session_id=session_id)
            return
        candidate.status = ActionStatus.EXECUTING
        await self._store.save_action(candidate)
        async def still_current() -> bool:
            send_now = self._clock.now()
            if revision != await self._store.get_revision(session_id):
                await self._audit("pre_send_blocked", reason="新用户消息使发送失效")
                return False
            if candidate.expires_at is not None and send_now > candidate.expires_at:
                await self._audit("pre_send_blocked", reason="candidate expired")
                return False
            if daily_event_id and (
                not self._evaluator.in_send_window(send_now)
                or datetime.fromtimestamp(send_now).date().isoformat() != daily_event_day
            ):
                await self._audit("pre_send_blocked", reason="生成期间已跨日或离开发送时段")
                return False
            if scheduled is None:
                contact = await unanswered_status(self._store, self._config, session_id, last_interaction, send_now)
                await self._audit("unanswered", check="pre_send", **contact)
                if not contact["allowed"]:
                    await self._audit("pre_send_blocked", reason=contact["reason_code"],
                                      reason_code=contact["reason_code"],
                                      next_evaluable_at=contact["next_evaluable_at"])
                    return False
            allowed, reason = await self._cooldown.check(
                user_id, candidate.topic, send_now, session_id=session_id,
            )
            if not allowed:
                await self._audit("pre_send_blocked", reason=reason)
            return allowed

        confirmed_text = ""
        async def record_delivered_text(text: str) -> None:
            nonlocal confirmed_text
            confirmed_text = text
            if self._delivery is not None:
                payload = self._delivery.payload(agent_event, candidate, text, response.text, selected,
                                                 daily_event_day, daily_event_id, scheduled.id if scheduled else None)
                await self._delivery.commit(payload)
                await self._audit("delivery_commit")
                return
            await self._sessions.append_and_persist(
                session_id,
                ConversationTurn(user_text="", assistant_text=text, timestamp=now),
            )
            await self._audit("delivery_commit")

        await self._audit("send_start")
        try:
            send_result = await self._sender.send(
                session_id, response.text,
                before_send=still_current,
                on_delivered=record_delivered_text,
            )
        except Exception as exc:
            await self._audit("platform_error", error_type=type(exc).__name__)
            self._logger.exception("Alice proactive sender failed safely")
            send_result = False
        sent = bool(send_result)
        delivered_chunks = int(getattr(send_result, "delivered_chunks", int(sent)))
        partial = bool(getattr(send_result, "partial", False))
        delivered = sent or partial or delivered_chunks > 0 or bool(confirmed_text)
        await self._audit("send_result", status="accepted" if sent else "partial" if delivered else "not_accepted",
                          delivered_chunks=delivered_chunks)
        if self._delivery is not None:
            if delivered:
                text = confirmed_text or getattr(send_result, "delivered_text", "")
                if text:
                    try:
                        await record_delivered_text(text)
                    except Exception as exc:
                        await self._audit("delivery_pending", error_type=type(exc).__name__)
                        self._logger.exception("Alice proactive local writes are pending recovery")
                else:
                    self._logger.warning("Alice proactive delivery has no confirmed transcript")
                self.metrics.proactive_messages_sent += 1
            else:
                candidate.status = ActionStatus.CANCELLED
                await self._store.save_action(candidate)
                if scheduled:
                    await self._scheduler.mark_evaluated(scheduled.id)
                await self._do_nothing("sender rejected proactive action", session_id=session_id)
            return
        if daily_event_id and daily_event_day and delivered:
            await self._store.record_daily_proactive_delivery(
                session_id, daily_event_day, daily_event_id
            )
        if sent or partial:
            await self._cooldown.record(user_id, session_id, candidate.topic, now)
        if sent:
            candidate.status = ActionStatus.COMPLETED
            self.metrics.proactive_messages_sent += 1
            await self._memory.record_access(selected)
            self._logger.info(
                f"Proactive message executed action_id={candidate.action_id} session={session_id}"
            )
        elif partial:
            candidate.status = ActionStatus.PARTIAL
            self.metrics.proactive_messages_sent += 1
            await self._memory.record_access(selected)
            self._logger.warning(
                f"Proactive message partially delivered action_id={candidate.action_id} "
                f"session={session_id} chunks={delivered_chunks}"
            )
        else:
            candidate.status = ActionStatus.CANCELLED
            await self._do_nothing("sender rejected proactive action", session_id=session_id)
        await self._store.save_action(candidate)
        if scheduled:
            await self._scheduler.mark_evaluated(scheduled.id)

    async def close(self) -> None:
        await self.stop()
        await self._store.close()
