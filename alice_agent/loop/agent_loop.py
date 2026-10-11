from __future__ import annotations

import asyncio
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
    ) -> None:
        self._config = config
        self._bus = event_bus
        self._clock = clock
        self._store = store
        self._scheduler = scheduler
        self._drives = drive_manager
        self._states = state_manager
        self._daily_events = daily_events
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
        self._decision_gate = decision_gate
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.metrics = LoopMetrics()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._config.agent_loop_enabled or self.running:
            return
        self._stopping = False
        await self._bus.publish(new_event(
            EventType.SYSTEM_START, source="alice_loop", timestamp=self._clock.now()
        ))
        self._task = asyncio.create_task(self._run(), name="alice-agent-loop")
        self._logger.info("Alice Agent Loop started")

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
            except Exception:
                consecutive_errors += 1
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

    async def _do_nothing(self, reason: str, *, score: float | None = None) -> None:
        self.metrics.do_nothing_count += 1
        suffix = "" if score is None else f" score={score:.3f}"
        self._logger.debug(f"PROACTIVE decision=DO_NOTHING reason={reason}{suffix}")

    async def _evaluate_session(
        self, tick_event: AgentEvent, scheduled: ScheduledContextEvent | None
    ) -> None:
        now = tick_event.timestamp
        session_id = tick_event.session_id
        user_id = tick_event.user_id
        last_tick = float(tick_event.payload.get("last_tick_at", now))
        elapsed = max(0.0, now - last_tick)
        rows = await self._store.list_sessions()
        row = next((item for item in rows if item["session_id"] == session_id), {})
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
            if self._daily_events is not None and recently_active:
                try:
                    events = await self._daily_events.ensure(
                        session_id, user_id, now, history
                    )
                    if isinstance(events, list):
                        daily_events = [item for item in events if isinstance(item, dict)]
                except Exception:
                    self._logger.exception("Alice daily event tick failed safely")
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
            await self._do_nothing("active conversation window")
            return
        in_send_window = self._evaluator.in_send_window(now)
        if last_interaction is not None and scheduled is None:
            send_stats = await self._store.send_stats(
                user_id, None, now - 86400.0, session_id
            )
            last_proactive = send_stats.get("session_last")
            if last_proactive is not None and last_interaction <= float(last_proactive):
                await self._do_nothing("previous proactive message is unanswered")
                return

        daily_event: dict[str, object] | None = None
        if daily_events and recently_active and in_send_window and not scheduled:
            local_day = datetime.fromtimestamp(now).date().isoformat()
            daily_state = await self._store.get_daily_proactive_state(session_id, local_day)
            attempts = int(daily_state.get("attempt_count", 0))
            attempt_limit = max(0, int(getattr(self._config, "proactive_daily_attempt_limit", 2)))
            attempted = set(daily_state.get("attempted_event_ids", []))
            delivered = set(daily_state.get("delivered_event_ids", []))
            if attempts < attempt_limit:
                daily_event = next((item for item in daily_events if (
                    str(item.get("event_id", ""))
                    and str(item.get("event_id", "")) not in attempted
                    and str(item.get("event_id", "")) not in delivered
                )), None)

        candidate = self._evaluator.evaluate(
            drives, availability, now, scheduled, daily_event=daily_event
        )
        if candidate.action_type == CandidateActionType.DO_NOTHING:
            await self._do_nothing(candidate.reason)
            return
        self.metrics.candidate_actions += 1
        candidate.metadata["session_id"] = session_id
        await self._store.save_action(candidate)

        if candidate.expires_at is not None and now > candidate.expires_at:
            candidate.status = ActionStatus.EXPIRED
            await self._store.save_action(candidate)
            await self._do_nothing("candidate expired")
            return
        allowed, cooldown_reason = await self._cooldown.check(
            user_id, candidate.topic, now, session_id=session_id
        )
        if not allowed:
            if cooldown_reason == "daily_budget":
                self.metrics.blocked_by_daily_budget += 1
            else:
                self.metrics.blocked_by_cooldown += 1
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            if scheduled:
                await self._scheduler.mark_evaluated(scheduled.id)
            await self._do_nothing(cooldown_reason)
            return
        is_daily_event = bool(candidate.metadata.get("daily_event_id"))
        daily_threshold = float(getattr(self._config, "proactive_daily_score_threshold", 0.45))
        decision = self._scorer.score(
            candidate, drives, relationship,
            threshold_override=daily_threshold if is_daily_event else None,
        )
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
            await self._do_nothing(decision.reason, score=decision.score)
            return

        daily_event_id = str(candidate.metadata.get("daily_event_id", ""))
        daily_event_day = str(candidate.metadata.get("daily_event_day", ""))
        if daily_event_id and daily_event_day:
            attempt_limit = max(0, int(getattr(self._config, "proactive_daily_attempt_limit", 2)))
            claimed = await self._store.claim_daily_proactive_attempt(
                session_id, daily_event_day, daily_event_id, attempt_limit
            )
            if not claimed:
                candidate.status = ActionStatus.CANCELLED
                await self._store.save_action(candidate)
                await self._do_nothing("daily proactive attempt limit reached")
                return

        revision = await self._store.get_revision(session_id)
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
        try:
            memories = await self._memory.recall(agent_event, perception, state, relationship)
        except Exception:
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("memory recall failed")
            return

        gate_decision = None
        if self._decision_gate is not None:
            gate_decision = await self._decision_gate.evaluate(
                candidate, drives, relationship, memories, availability, agent_event.trace_id
            )
            if gate_decision.action in {"do_nothing", "defer"}:
                candidate.status = ActionStatus.CANCELLED
                await self._store.save_action(candidate)
                if scheduled:
                    await self._scheduler.mark_evaluated(scheduled.id)
                await self._do_nothing(gate_decision.reason, score=decision.score)
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
            plan = await self._planner.plan(
                session_id, candidate, state, relationship, drives, memories, availability
            )
            if gate_decision is not None and gate_decision.shadow_action is not None:
                existing_action = "send" if plan.action == "proactive_message" else "do_nothing"
                self._decision_gate.record_shadow_comparison(
                    agent_event.trace_id, gate_decision.shadow_action, existing_action
                )
        if plan.action != "proactive_message":
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            if scheduled:
                await self._scheduler.mark_evaluated(scheduled.id)
            await self._do_nothing(plan.reason or "planner chose silence", score=decision.score)
            return
        if revision != await self._store.get_revision(session_id):
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("new user event invalidated action")
            return

        selected = self._memory.select_for_response(memories, plan.memory_ids)
        action_plan = ActionPlan(
            action="recall_then_reply" if selected else "reply",
            intent="proactive_follow_up", response_goal=plan.response_goal or candidate.reason,
            memory_ids=[item.id for item in selected], source="proactive",
        )
        history = await self._sessions.load_history(session_id)
        try:
            response = await self._responses.generate(
                agent_event, perception, state, selected, action_plan,
                history, relationship,
            )
        except Exception:
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("response generation failed safely")
            return
        if revision != await self._store.get_revision(session_id):
            candidate.status = ActionStatus.CANCELLED
            await self._store.save_action(candidate)
            await self._do_nothing("new user event invalidated generated action")
            return
        if not response.text:
            await self._do_nothing("empty proactive response")
            return
        candidate.status = ActionStatus.EXECUTING
        await self._store.save_action(candidate)
        async def still_current() -> bool:
            return revision == await self._store.get_revision(session_id)

        async def record_delivered_text(text: str) -> None:
            await self._sessions.append_and_persist(
                session_id,
                ConversationTurn(user_text="", assistant_text=text, timestamp=now),
            )

        try:
            send_result = await self._sender.send(
                session_id, response.text,
                before_send=still_current,
                on_delivered=record_delivered_text,
            )
        except Exception:
            self._logger.exception("Alice proactive sender failed safely")
            send_result = False
        sent = bool(send_result)
        delivered_chunks = int(getattr(send_result, "delivered_chunks", int(sent)))
        partial = bool(getattr(send_result, "partial", False))
        delivered = sent or partial or delivered_chunks > 0
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
            await self._do_nothing("sender rejected proactive action")
        await self._store.save_action(candidate)
        if scheduled:
            await self._scheduler.mark_evaluated(scheduled.id)

    async def close(self) -> None:
        await self.stop()
        await self._store.close()
