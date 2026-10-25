from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.storage.in_memory import InMemoryStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.drives.manager import DriveManager
from astrbot_plugin_alice_agent.alice_agent.drives.models import DriveState
from astrbot_plugin_alice_agent.alice_agent.drives.reducer import DriveReducer
from astrbot_plugin_alice_agent.alice_agent.events.bus import EventBus
from astrbot_plugin_alice_agent.alice_agent.events.models import new_event
from astrbot_plugin_alice_agent.alice_agent.events.types import EventType
from astrbot_plugin_alice_agent.alice_agent.loop.agent_loop import AgentLoop
from astrbot_plugin_alice_agent.alice_agent.loop.scheduler import ContextScheduler
from astrbot_plugin_alice_agent.alice_agent.loop.store import InMemoryAutonomyStore, SQLiteAutonomyStore
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.ports.proactive import ProactiveSendResult
from astrbot_plugin_alice_agent.alice_agent.proactive.cooldown import CooldownConfig, CooldownManager
from astrbot_plugin_alice_agent.alice_agent.proactive.daily_policy import DailyProactivePolicy
from astrbot_plugin_alice_agent.alice_agent.proactive.evaluator import ProactiveEvaluator
from astrbot_plugin_alice_agent.alice_agent.proactive.models import (
    ActionStatus, CandidateAction, CandidateActionType, ProactiveActionPlan,
    ScheduledContextEvent, UserAvailability,
)
from astrbot_plugin_alice_agent.alice_agent.proactive.scorer import ActionScoreConfig, ActionScorer
from astrbot_plugin_alice_agent.alice_agent.proactive.planner import ProactivePlanner
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.llm.service import LLMService
from astrbot_plugin_alice_agent.alice_agent.relationship.manager import RelationshipManager
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState
from astrbot_plugin_alice_agent.alice_agent.relationship.reducer import RelationshipReducer
from astrbot_plugin_alice_agent.alice_agent.relationship.store import InMemoryRelationshipStore
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer
from astrbot_plugin_alice_agent.alice_agent.state.time_reducer import TimeStateReducer


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class BlockingClock(FakeClock):
    def __init__(self, value: float = 0.0) -> None:
        super().__init__(value)
        self.sleeping = __import__("asyncio").Event()
        self.release = __import__("asyncio").Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await self.release.wait()


class RaisingLLM:
    async def generate(self, request):
        raise RuntimeError("provider failed")

    async def close(self):
        return None


class FakeMemory:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def recall(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("memory unavailable")
        return []

    def select_for_response(self, memories, ids):
        return []

    async def record_access(self, memories):
        return None


class FakePlanner:
    def __init__(self, action: str = "proactive_message") -> None:
        self.action = action
        self.calls = 0

    async def plan(self, *args, **kwargs):
        self.calls += 1
        if self.action == "proactive_message":
            return ProactiveActionPlan(
                action="proactive_message", reason="natural follow-up",
                topic="考试", response_goal="自然询问考试后的感受",
            )
        return ProactiveActionPlan.do_nothing("not enough context")


class FakeResponses:
    async def generate(self, event, *args, **kwargs):
        return AgentResponse(text="考试结束了吗？之前准备了那么久，希望还顺利。", trace_id=event.trace_id)


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, session_id: str, text: str, *, before_send=None, on_delivered=None):
        if before_send is not None and not await before_send():
            return ProactiveSendResult(False)
        self.messages.append((session_id, text))
        if on_delivered is not None:
            await on_delivered(text)
        return ProactiveSendResult(True, 1, text)


class PartialFakeSender(FakeSender):
    async def send(self, session_id: str, text: str, *, before_send=None, on_delivered=None):
        if before_send is not None and not await before_send():
            return ProactiveSendResult(False)
        delivered_text = "只发出第一段。"
        self.messages.append((session_id, delivered_text))
        if on_delivered is not None:
            await on_delivered(delivered_text)
        return ProactiveSendResult(False, 1, delivered_text)


class FakeJevGate:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, *args, **kwargs):
        self.calls += 1
        return type("GateDecision", (), {"action": "send", "reason": "mock", "shadow_action": None})()


class FakeDailyEvents:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.calls = 0

    async def ensure(self, *args, **kwargs):
        self.calls += 1
        return self.events

    async def read_cached(self, *args, **kwargs):
        return self.events


def build_loop(clock: FakeClock, *, daily_budget: int = 2, active_window: float = 0.0):
    config = AgentConfig(
        proactive_score_threshold=0.0,
        proactive_defer_threshold=0.0,
        proactive_min_interval=0.0,
        proactive_same_topic_interval=0.0,
        proactive_daily_budget=daily_budget,
        active_conversation_window=active_window,
    )
    store = InMemoryAutonomyStore()
    sessions = SessionManager()
    relationship = RelationshipManager(
        InMemoryRelationshipStore(), RelationshipReducer(), enabled=True
    )
    planner = FakePlanner()
    sender = FakeSender()
    loop = AgentLoop(
        config=config, event_bus=EventBus(), clock=clock, store=store,
        scheduler=ContextScheduler(store),
        drive_manager=DriveManager(store, DriveReducer()),
        state_manager=StateManager(InMemoryStorageAdapter(), StateReducer(), TimeStateReducer()),
        daily_policy=DailyProactivePolicy(store, random_value=lambda: 0.0),
        relationship_manager=relationship, memory=FakeMemory(),
        proactive_planner=planner, response_generator=FakeResponses(),
        scorer=ActionScorer(ActionScoreConfig(threshold=0.0, defer_threshold=0.0)),
        evaluator=ProactiveEvaluator("00:00", "23:59:59"),
        cooldown=CooldownManager(store, CooldownConfig(0.0, 0.0, daily_budget)),
        sender=sender, session_manager=sessions, logger=NullLogger(),
    )
    return loop, store, planner, sender


class EventAndTimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_window_supports_local_and_cross_midnight_ranges(self) -> None:
        def stamp(day: int, hour: int, minute: int = 0) -> float:
            return datetime(2026, 9, day, hour, minute).timestamp()

        daytime = ProactiveEvaluator("09:00", "21:00")
        self.assertFalse(daytime.in_send_window(stamp(29, 8, 59)))
        self.assertTrue(daytime.in_send_window(stamp(29, 9)))
        self.assertTrue(daytime.in_send_window(stamp(29, 21)))
        self.assertFalse(daytime.in_send_window(stamp(29, 21, 1)))

        overnight = ProactiveEvaluator("22:00", "06:00")
        self.assertTrue(overnight.in_send_window(stamp(29, 23)))
        self.assertTrue(overnight.in_send_window(stamp(29, 2)))
        self.assertTrue(overnight.in_send_window(stamp(29, 6)))
        self.assertFalse(overnight.in_send_window(stamp(29, 12)))

    async def test_event_bus_deduplicates_event_ids(self) -> None:
        bus = EventBus()
        event = new_event(EventType.TIME_TICK, 1.0)
        self.assertTrue(await bus.publish(event))
        self.assertFalse(await bus.publish(event))
        self.assertEqual(len(bus.drain_nowait()), 1)

    async def test_time_reducer_uses_injected_elapsed_time_and_is_bounded(self) -> None:
        reducer = TimeStateReducer()
        state = AgentState(mood_arousal=1.0, energy=1.0, activity="chatting")
        one = reducer.reduce(state, 3600)
        five = reducer.reduce(state, 5 * 3600)
        day = reducer.reduce(state, 24 * 3600)
        self.assertGreater(one.mood_arousal, five.mood_arousal)
        self.assertGreaterEqual(day.mood_arousal, 0.0)
        self.assertLessEqual(day.energy, 1.0)
        self.assertEqual(one.activity, "idle")

    async def test_drive_changes_are_bounded_over_1h_5h_1d(self) -> None:
        reducer = DriveReducer()
        relationship = RelationshipState("u", closeness=0.8)
        state = AgentState(mood_arousal=0.8)
        initial = DriveState("s", "u", updated_at=0.0)
        one = reducer.advance(initial, 3600, state, relationship, unresolved_context_count=1, now=3600)
        five = reducer.advance(initial, 5 * 3600, state, relationship, unresolved_context_count=1, now=5*3600)
        day = reducer.advance(initial, 24 * 3600, state, relationship, unresolved_context_count=1, now=24*3600)
        self.assertLess(one.social_need, five.social_need)
        self.assertLess(five.curiosity, day.curiosity)
        for value in (day.social_need, day.curiosity, day.rest_need):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class ProactivePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_event_reaches_planner_and_is_recorded_in_history(self) -> None:
        now = datetime(2026, 9, 29, 12, 0).timestamp()
        clock = FakeClock(now)
        loop, store, planner, sender = build_loop(clock)
        loop._daily_events = FakeDailyEvents([{
            "event_id": "daily-1", "day": "2026-09-29",
            "text": "Alice读到一个有趣的问题，花了一会儿琢磨。",
            "topic_hook": "今天读到的问题", "importance": 0.35,
        }])
        await loop.on_user_event(AgentEvent(
            "recent", "t", "USER_MESSAGE", "test", "daily-session", "u",
            now - 3600, "我们聊了会儿书。",
        ))
        await store.save_drive(DriveState(
            "daily-session", "u", social_need=0.8, curiosity=0.1, rest_need=0.05,
        ))

        await loop.tick()

        self.assertEqual(planner.calls, 1)
        self.assertEqual(len(sender.messages), 1)
        history = loop._sessions.history("daily-session")
        self.assertEqual(history[-1].user_text, "")
        self.assertEqual(history[-1].assistant_text, "考试结束了吗？之前准备了那么久，希望还顺利。")
        state = await store.get_daily_proactive_state("daily-session", "2026-09-29")
        self.assertEqual(state["attempt_count"], 1)
        self.assertEqual(state["delivered_event_ids"], ["daily-1"])

        await loop.tick()
        self.assertEqual(planner.calls, 1)

    async def test_rejected_daily_plan_is_attempted_at_most_twice_per_day(self) -> None:
        now = datetime(2026, 9, 29, 12, 0).timestamp()
        clock = FakeClock(now)
        loop, store, planner, _ = build_loop(clock)
        planner.action = "do_nothing"
        loop._daily_events = FakeDailyEvents([
            {"event_id": "daily-1", "day": "2026-09-29", "text": "Alice读到一个问题。", "topic_hook": "读到的问题", "importance": 0.35},
            {"event_id": "daily-2", "day": "2026-09-29", "text": "Alice完成了一件小事。", "topic_hook": "完成的小事", "importance": 0.35},
        ])
        await loop.on_user_event(AgentEvent(
            "recent", "t", "USER_MESSAGE", "test", "attempt-session", "u",
            now - 3600, "最近怎么样？",
        ))
        await store.save_drive(DriveState(
            "attempt-session", "u", social_need=0.8, curiosity=0.1, rest_need=0.05,
        ))

        for _ in range(4):
            await loop.tick()

        self.assertEqual(planner.calls, 2)
        state = await store.get_daily_proactive_state("attempt-session", "2026-09-29")
        self.assertEqual(state["attempt_count"], 2)
        self.assertEqual(state["attempted_event_ids"], ["daily-1", "daily-2"])

    async def test_partial_daily_delivery_is_deduplicated_and_counted_as_unanswered(self) -> None:
        now = datetime(2026, 9, 29, 12, 0).timestamp()
        clock = FakeClock(now)
        loop, store, planner, sender = build_loop(clock)
        sender = PartialFakeSender()
        loop._sender = sender
        loop._daily_events = FakeDailyEvents([{
            "event_id": "daily-partial", "day": "2026-09-29",
            "text": "Alice完成了一件小事，心里稍微轻松了些。",
            "topic_hook": "今天完成的小事", "importance": 0.35,
        }])
        await loop.on_user_event(AgentEvent(
            "recent", "t", "USER_MESSAGE", "test", "partial-session", "u",
            now - 3600, "最近怎么样？",
        ))
        await store.save_drive(DriveState(
            "partial-session", "u", social_need=0.5, curiosity=0.2, rest_need=0.1,
        ))

        await loop.tick()
        state = await store.get_daily_proactive_state("partial-session", "2026-09-29")
        stats = await store.send_stats("u", None, now - 86400, "partial-session")
        self.assertEqual(state["delivered_event_ids"], ["daily-partial"])
        self.assertEqual(stats["session_last"], now)
        self.assertEqual(loop._sessions.history("partial-session")[-1].assistant_text, "只发出第一段。")

        await loop.tick()
        self.assertEqual(planner.calls, 1)

    async def test_inactive_session_can_still_run_an_explicit_scheduled_follow_up(self) -> None:
        now = datetime(2026, 9, 29, 12, 0).timestamp()
        clock = FakeClock(now)
        loop, _, planner, sender = build_loop(clock)
        await loop.on_user_event(AgentEvent(
            "old", "t", "USER_MESSAGE", "test", "old-session", "u",
            now - 8 * 86400, "下周记得问我考试结果。",
        ))
        await loop.schedule(ScheduledContextEvent(
            "follow-up", "u", "old-session", "exam", "考试", "考试之后的情况",
            now, now, now + 3600,
        ))

        await loop.tick()

        self.assertEqual(planner.calls, 1)
        self.assertEqual(len(sender.messages), 1)

    async def test_future_schedule_grows_curiosity_without_calling_llm_on_tick(self) -> None:
        clock = FakeClock(10_000)
        loop, store, planner, sender = build_loop(clock)
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", 6400, "下午考试"))
        await loop.schedule(ScheduledContextEvent(
            "future", "u", "s", "exam", "考试", "下午考试",
            20_000, 20_000, 30_000,
        ))
        await loop.tick()
        drive = await store.get_drive("s")
        self.assertGreater(drive.curiosity, 0.0)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(sender.messages, [])

    async def test_duplicate_user_event_is_ignored(self) -> None:
        clock = FakeClock(1000)
        loop, store, _, _ = build_loop(clock)
        event = AgentEvent("same", "t", "USER_MESSAGE", "test", "s", "u", 1000, "hi")
        self.assertTrue(await loop.on_user_event(event))
        revision = await store.get_revision("s")
        self.assertFalse(await loop.on_user_event(event))
        self.assertEqual(await store.get_revision("s"), revision)

    async def test_loop_start_is_idempotent_and_stop_cancels_task(self) -> None:
        clock = BlockingClock(1000)
        loop, _, _, _ = build_loop(clock)
        await loop.start()
        first_task = loop._task
        await loop.start()
        self.assertIs(loop._task, first_task)
        await clock.sleeping.wait()
        self.assertTrue(loop.running)
        await loop.stop()
        self.assertFalse(loop.running)

    async def test_proactive_llm_failure_defaults_to_do_nothing(self) -> None:
        planner = ProactivePlanner(
            LLMService(RaisingLLM(), 1), ContextBuilder(), timeout=1
        )
        plan = await planner.plan(
            "s",
            CandidateAction(CandidateActionType.MEMORY_FOLLOW_UP, "exam", relevance=1),
            AgentState(), RelationshipState("u"), DriveState("s", "u"), [],
            UserAvailability(),
        )
        self.assertEqual(plan.action, "do_nothing")

    async def test_memory_failure_never_produces_proactive_fallback(self) -> None:
        clock = FakeClock(1000)
        loop, _, planner, sender = build_loop(clock)
        loop._memory = FakeMemory(fail=True)
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", 0, "下午考试"))
        await loop.schedule(ScheduledContextEvent("x", "u", "s", "exam", "考试", "考试应已结束", 1000, 1000, 2000))
        await loop.tick()
        self.assertEqual(planner.calls, 0)
        self.assertEqual(sender.messages, [])

    async def test_recent_user_message_blocks_even_with_candidate(self) -> None:
        clock = FakeClock(1000.0)
        loop, _, planner, sender = build_loop(clock, active_window=900.0)
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", 1000.0, "hi"))
        await loop.schedule(ScheduledContextEvent("x", "u", "s", "exam", "考试", "下午考试", 1000, 1000, 2000))
        await loop.tick()
        self.assertEqual(planner.calls, 0)
        self.assertEqual(sender.messages, [])

    async def test_relationship_influences_score_but_not_cooldown(self) -> None:
        scorer = ActionScorer(ActionScoreConfig(threshold=0.5))
        candidate = CandidateAction(CandidateActionType.MEMORY_FOLLOW_UP, "exam", relevance=0.9, interruption_cost=0.3)
        drive = DriveState("s", "u", social_need=0.7, curiosity=0.8)
        low = scorer.score(candidate, drive, RelationshipState("u", familiarity=0, trust=0.2, closeness=0)).score
        high = scorer.score(candidate, drive, RelationshipState("u", familiarity=1, trust=1, closeness=1)).score
        self.assertGreater(high, low)
        store = InMemoryAutonomyStore()
        cooldown = CooldownManager(store, CooldownConfig(100, 100, 1))
        await cooldown.record("u", "s", "考试", 1000)
        allowed, reason = await cooldown.check("u", "考试", 1010)
        self.assertFalse(allowed)
        self.assertIn(reason, {"daily_budget", "global_cooldown", "user_cooldown", "same_topic_cooldown"})

    async def test_jev_send_cannot_bypass_code_cooldown(self) -> None:
        clock = FakeClock(1000)
        loop, store, planner, sender = build_loop(clock)
        gate = FakeJevGate()
        loop._decision_gate = gate
        loop._cooldown = CooldownManager(store, CooldownConfig(100, 100, 10))
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", 0, "下午考试"))
        await loop._cooldown.record("u", "s", "考试", 999)
        await loop.schedule(ScheduledContextEvent("x", "u", "s", "exam", "考试", "考试应已结束", 1000, 1000, 2000))
        await loop.tick()
        self.assertEqual(gate.calls, 0)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(sender.messages, [])

    async def test_user_event_cancels_pending_action(self) -> None:
        clock = FakeClock(1000)
        loop, store, _, _ = build_loop(clock)
        action = CandidateAction(CandidateActionType.PROACTIVE_MESSAGE, "pending", metadata={"session_id": "s"}, status=ActionStatus.PLANNED)
        await store.save_action(action)
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", 1000, "回来啦"))
        self.assertEqual(store.actions[action.action_id].status, ActionStatus.CANCELLED)

    async def test_24h_ticks_respect_daily_budget_and_expose_metrics(self) -> None:
        clock = FakeClock(2_000_000_000.0)
        loop, _, planner, sender = build_loop(clock, daily_budget=2)
        await loop.on_user_event(AgentEvent("seed", "t", "USER_MESSAGE", "test", "s", "u", clock.now()-3600, "下午要考试"))
        for hour in range(24):
            now = clock.now()
            await loop.schedule(ScheduledContextEvent(
                f"exam-{hour}", "u", "s", "future_event", f"考试-{hour}",
                "用户之前提到的考试应该已经结束", now, now, now + 1800,
            ))
            await loop.tick()
            clock.advance(3600)
        self.assertEqual(len(sender.messages), 2)
        self.assertEqual(planner.calls, 2)
        self.assertEqual(loop.metrics.total_ticks, 24)
        self.assertEqual(loop.metrics.candidate_actions, 24)
        self.assertEqual(loop.metrics.proactive_messages_sent, 2)
        self.assertEqual(loop.metrics.blocked_by_daily_budget, 22)
        self.assertEqual(loop.metrics.do_nothing_count, 22)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_proactive_attempt_and_delivery_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "autonomy.db"
            first = SQLiteAutonomyStore(path)
            self.assertTrue(await first.claim_daily_proactive_attempt(
                "s", "2026-09-29", "daily-event", 2,
            ))
            await first.record_daily_proactive_delivery("s", "2026-09-29", "daily-event")
            await first.record_send("u", "s", "today", 1000.0)
            await first.close()

            second = SQLiteAutonomyStore(path)
            state = await second.get_daily_proactive_state("s", "2026-09-29")
            stats = await second.send_stats("u", "today", 0.0, "s")
            self.assertEqual(state["attempt_count"], 1)
            self.assertEqual(state["attempted_event_ids"], ["daily-event"])
            self.assertEqual(state["delivered_event_ids"], ["daily-event"])
            self.assertEqual(stats["session_last"], 1000.0)
            self.assertFalse(await second.claim_daily_proactive_attempt(
                "s", "2026-09-29", "daily-event", 2,
            ))
            await second.close()

    async def test_drive_cooldown_and_schedule_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "autonomy.db"
            first = SQLiteAutonomyStore(path)
            await first.save_drive(DriveState("s", "u", social_need=0.7, updated_at=1000))
            event = ScheduledContextEvent("future", "u", "s", "exam", "考试", "summary", 1200, 1200, 1800)
            await first.add_scheduled(event)
            await first.record_send("u", "s", "考试", 1000)
            await first.close()

            second = SQLiteAutonomyStore(path)
            restored = await second.get_drive("s")
            due = await second.due_scheduled(1300)
            stats = await second.send_stats("u", "考试", 0)
            self.assertAlmostEqual(restored.social_need, 0.7)
            self.assertEqual([item.id for item in due], ["future"])
            self.assertEqual(stats["daily_count"], 1)
            await second.close()


if __name__ == "__main__":
    unittest.main()
