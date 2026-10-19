from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.loop.store import InMemoryAutonomyStore, SQLiteAutonomyStore
from astrbot_plugin_alice_agent.alice_agent.proactive.daily_policy import DailyProactivePolicy
from astrbot_plugin_alice_agent.alice_agent.proactive.scorer import ActionScorer
from astrbot_plugin_alice_agent.tests.test_autonomy import FakeClock, FakeDailyEvents, build_loop


DAY = "2026-10-19"
NOW = datetime(2026, 10, 19, 12).timestamp()
EVENTS = [{"event_id": "today", "day": DAY, "text": "Alice完成了今天的剑术练习。",
           "topic_hook": "今天的练习", "importance": 0.35, "category": "small_success"}]


class Model:
    def __init__(self, text='{"willingness": 0.8, "reason": "想分享今天的小事"}'):
        self.text, self.calls = text, 0

    async def generate(self, request):
        self.calls += 1
        self.request = request
        return LLMResult(self.text)


class DailyProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_mood_and_event_importance_change_probability(self):
        policy = DailyProactivePolicy(InMemoryAutonomyStore())
        good = AgentState(mood_valence=0.8, energy=0.8, stress=0.1)
        bad = AgentState(mood_valence=-0.8, energy=0.2, stress=0.9)
        self.assertGreater(policy.rule_probability(good, EVENTS), policy.rule_probability(bad, EVENTS))
        meaningful = [{**EVENTS[0], "importance": 0.9}]
        self.assertGreater(policy.rule_probability(good, meaningful), policy.rule_probability(good, EVENTS))
        self.assertEqual(policy.rule_probability(good, []), 0)

    async def test_llm_gets_mood_events_and_is_cached_even_on_silence(self):
        store, model = InMemoryAutonomyStore(), Model()
        policy = DailyProactivePolicy(store, llm=model, random_value=lambda: 0.99)
        first = await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW)
        for _ in range(20):
            self.assertEqual(first, await policy.evaluate("s", DAY, AgentState(mood_valence=1), EVENTS, NOW))
        self.assertFalse(first["eligible"])
        self.assertEqual(first["source"], "llm_blend")
        self.assertEqual(model.calls, 1)
        payload = json.loads(model.request.prompt)
        self.assertEqual(payload["events"][0]["text"], EVENTS[0]["text"])
        self.assertIn("mood_valence", payload["mood"])

    async def test_invalid_or_failed_model_falls_back_once(self):
        for text in ('bad json', '{"willingness": true}', '{"willingness": NaN}', '{"willingness": 2}'):
            model = Model(text)
            policy = DailyProactivePolicy(InMemoryAutonomyStore(), llm=model, random_value=lambda: 0)
            decision = await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW)
            self.assertEqual(decision["source"], "rule_fallback")
            self.assertTrue(decision["eligible"])
            await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW)
            self.assertEqual(model.calls, 1)

    async def test_timeout_and_cancellation(self):
        class Blocked:
            async def generate(self, request):
                await asyncio.Event().wait()
        policy = DailyProactivePolicy(InMemoryAutonomyStore(), llm=Blocked(), timeout=0.01)
        self.assertEqual((await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW))["source"], "rule_fallback")
        policy = DailyProactivePolicy(InMemoryAutonomyStore(), llm=Blocked(), timeout=10)
        task = asyncio.create_task(policy.evaluate("s", DAY, AgentState(), EVENTS, NOW))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await policy._store.get_daily_proactive_decision("s", DAY))

    async def test_sqlite_restart_preserves_draw_and_new_day_rechecks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autonomy.db"
            original = SQLiteAutonomyStore(path)
            first = await DailyProactivePolicy(original, random_value=lambda: 0.99).evaluate("s", DAY, AgentState(), EVENTS, NOW)
            restored = SQLiteAutonomyStore(path)
            model = Model()
            policy = DailyProactivePolicy(restored, llm=model, random_value=lambda: 0)
            self.assertEqual(first, await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW))
            self.assertEqual(model.calls, 0)
            self.assertTrue((await policy.evaluate("s", "2026-10-20", AgentState(), EVENTS, NOW + 86400))["eligible"])
            self.assertEqual(model.calls, 1)

    async def _loop(self, roll=0):
        loop, store, planner, sender = build_loop(FakeClock(NOW))
        loop._daily_events = FakeDailyEvents([dict(event) for event in EVENTS])
        loop._config = replace(loop._config, daily_events_enabled=True)
        model = Model()
        loop._daily_policy = DailyProactivePolicy(store, llm=model, random_value=lambda: roll)
        await loop.on_user_event(AgentEvent("e", "trace", "USER_MESSAGE", "test", "s", "u", NOW - 3600, "你好"))
        return loop, store, planner, sender, model

    async def test_default_score_reaches_sender_without_inflated_drives(self):
        loop, store, planner, sender, model = await self._loop()
        loop._scorer = ActionScorer()
        await loop.tick()
        self.assertEqual(planner.calls, 1)
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(model.calls, 1)
        self.assertTrue((await store.get_daily_proactive_decision("s", DAY))["eligible"])

    async def test_failed_draw_never_retries_per_tick_or_per_event(self):
        loop, store, planner, sender, model = await self._loop(0.99)
        loop._daily_events.events.append({**EVENTS[0], "event_id": "second"})
        try:
            for _ in range(20):
                await loop.tick()
            self.assertEqual(model.calls, 1)
            self.assertEqual(planner.calls, 0)
            self.assertFalse(sender.messages)
            self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempt_count"], 0)
        finally:
            loop._daily_events.events.pop()

    async def test_probability_never_bypasses_cooldown_or_active_window(self):
        loop, store, planner, sender, model = await self._loop()
        loop._config = replace(loop._config, active_conversation_window=7200)
        await loop.tick()
        self.assertEqual(model.calls, 0)
        loop._config = replace(loop._config, active_conversation_window=0)
        await store.record_send("u", "s", "other", NOW - 1800)
        # Existing unanswered-message protection also remains effective.
        await loop.tick()
        self.assertEqual(model.calls, 0)
        self.assertFalse(sender.messages)

    async def test_user_input_during_daily_llm_cancels_old_action(self):
        loop, store, planner, sender, _ = await self._loop()
        class InterruptingModel(Model):
            async def generate(self, request):
                await loop.register_user_event(AgentEvent("new", "t", "USER_MESSAGE", "test", "s", "u", NOW, "等一下"))
                return await super().generate(request)
        loop._daily_policy = DailyProactivePolicy(store, llm=InterruptingModel(), random_value=lambda: 0)
        await loop.tick()
        self.assertEqual(planner.calls, 0)
        self.assertFalse(sender.messages)

    async def test_generation_crossing_send_window_is_not_sent(self):
        from astrbot_plugin_alice_agent.alice_agent.proactive.evaluator import ProactiveEvaluator
        loop, store, planner, sender, _ = await self._loop()
        loop._evaluator = ProactiveEvaluator("09:00", "12:00")
        original = loop._responses.generate
        async def slow_response(*args, **kwargs):
            loop._clock.advance(120)
            return await original(*args, **kwargs)
        loop._responses.generate = slow_response
        await loop.tick()
        self.assertEqual(planner.calls, 1)
        self.assertFalse(sender.messages)
        self.assertFalse((await store.get_daily_proactive_state("s", DAY))["delivered_event_ids"])

    async def test_provider_exception_falls_back_and_does_not_repeat(self):
        class Failed(Model):
            async def generate(self, request):
                self.calls += 1
                raise RuntimeError("provider unavailable")
        model, store = Failed(), InMemoryAutonomyStore()
        policy = DailyProactivePolicy(store, llm=model, random_value=lambda: 0)
        decision = await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW)
        self.assertEqual(decision["source"], "rule_fallback")
        self.assertEqual(decision, await policy.evaluate("s", DAY, AgentState(), EVENTS, NOW))
        self.assertEqual(model.calls, 1)

    async def test_model_intent_changes_probability_but_mood_still_matters(self):
        async def probability(text, state):
            policy = DailyProactivePolicy(InMemoryAutonomyStore(), llm=Model(text), random_value=lambda: 0)
            return (await policy.evaluate("s", DAY, state, EVENTS, NOW))["probability"]
        low = await probability('{"willingness":0}', AgentState())
        high = await probability('{"willingness":1}', AgentState())
        self.assertGreater(high, low)
        sad = await probability('{"willingness":0.5}', AgentState(mood_valence=-0.8))
        happy = await probability('{"willingness":0.5}', AgentState(mood_valence=0.8))
        self.assertGreater(happy, sad)
