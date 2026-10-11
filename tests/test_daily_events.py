from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.storage import SQLiteStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.daily_events.mood import DailyMoodReducer
from astrbot_plugin_alice_agent.alice_agent.daily_events.service import DailyEventService
from astrbot_plugin_alice_agent.alice_agent.decision.models import (
    DecisionAnswer, DecisionMode, DecisionResult,
)
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer


class EventLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        self.last_request = request
        return LLMResult("Alice按自己的节奏完成了一件日常小事。")


class ReactionLLM(EventLLM):
    def __init__(self, reaction: str) -> None:
        super().__init__()
        self.reaction = reaction

    async def generate(self, request):
        if request.stage == "daily_event_reaction":
            self.calls += 1
            self.last_request = request
            return LLMResult(self.reaction)
        return await super().generate(request)


class NegativeJev:
    async def decide(self, state, questions, **kwargs):
        return DecisionResult(
            "fixed", kwargs["trace_id"], kwargs["decision_type"],
            kwargs["schema_version"], "", "test-jev", 1.0,
            answers={
                key: DecisionAnswer("score", score=0.0, confidence=0.99)
                for key in questions
            },
        )

    async def close(self):
        return None


class PartialJev(NegativeJev):
    async def decide(self, state, questions, **kwargs):
        result = await super().decide(state, questions, **kwargs)
        result.answers.pop("stress")
        return result


class LowConfidenceJev(NegativeJev):
    async def decide(self, state, questions, **kwargs):
        result = await super().decide(state, questions, **kwargs)
        for answer in result.answers.values():
            answer.confidence = 0.60
        return result


class OneWeakAxisJev(NegativeJev):
    async def decide(self, state, questions, **kwargs):
        result = await super().decide(state, questions, **kwargs)
        result.answers["valence"].confidence = 0.30
        return result


class DailyMoodTests(unittest.TestCase):
    def test_today_experience_is_available_to_response_without_invented_memory(self) -> None:
        state = DailyMoodReducer().reduce(AgentState(), "2026-09-28", [
            {"text": "Alice读到一个有趣的问题。", "topic_hook": "今天读到的问题", "reaction": {}}
        ])
        event = AgentEvent(
            event_id="one", trace_id="trace", type="user_message", source="test",
            session_id="session", user_id="user", timestamp=1.0, text="你好",
        )
        system, prompt = ContextBuilder().for_response(
            event, PerceptionResult(), state, [], ActionPlan(), [],
        )
        self.assertIn("state.daily_events", system)
        payload = json.loads(prompt)
        self.assertEqual(payload["state"]["daily_events"], ["Alice读到一个有趣的问题。"])
        self.assertEqual(payload["selected_memories"], [])

    def test_inertia_recovery_and_event_effect_without_reset(self) -> None:
        reducer = DailyMoodReducer()
        previous = AgentState(mood_valence=-0.4, daily_event_day="2026-09-27")
        today = reducer.reduce(previous, "2026-09-28", [
            {"text": "完成小事。", "topic_hook": "今天的小事", "reaction": {"valence": 0.10}}
        ])
        self.assertAlmostEqual(today.mood_valence, -0.20)
        self.assertEqual(today.daily_events, ["完成小事。"])
        self.assertEqual(today.topic_hooks, ["今天的小事"])
        self.assertIs(reducer.reduce(today, "2026-09-28", []), today)
        tomorrow = reducer.reduce(today, "2026-09-29", [])
        self.assertAlmostEqual(tomorrow.mood_valence, -0.15)
        self.assertEqual(tomorrow.daily_events, [])

    def test_many_events_cannot_push_mood_out_of_range(self) -> None:
        event = {"reaction": {axis: 1.0 for axis in (
            "valence", "arousal", "dominance", "energy", "stress", "loneliness"
        )}}
        result = DailyMoodReducer().reduce(AgentState(), "2026-09-28", [event] * 3)
        self.assertLessEqual(result.mood_valence, 0.30)
        self.assertTrue(all(0.0 <= getattr(result, axis) <= 1.0 for axis in (
            "mood_arousal", "mood_dominance", "energy", "stress", "loneliness"
        )))


class DailyEventServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_weak_daily_axis_is_mixed_even_with_high_overall_confidence(self) -> None:
        from astrbot_plugin_alice_agent.adapters.storage import InMemoryStorageAdapter

        storage = InMemoryStorageAdapter()
        reaction = {axis: 0.08 for axis in (
            "valence", "arousal", "dominance", "energy", "stress", "loneliness"
        )}
        reaction["importance"] = 0.4
        llm = ReactionLLM(json.dumps(reaction))
        service = DailyEventService(
            storage, StateManager(storage, StateReducer()), llm, ContextBuilder(),
            DecisionService(OneWeakAxisJev(), mode=DecisionMode.PRIMARY),
            enabled=True,
        )
        scores, importance, source = await service._reaction(
            {"category": "routine"}, "Alice整理了手边的东西。",
            AgentState(), "trace",
        )
        self.assertEqual(source, "jev_llm_blend")
        self.assertAlmostEqual(scores["valence"], 0.0425)
        self.assertAlmostEqual(scores["arousal"], -0.12)
        self.assertAlmostEqual(importance, 0.0)
        self.assertEqual(llm.calls, 1)

    async def test_low_confidence_jev_daily_numbers_blend_with_llm(self) -> None:
        from astrbot_plugin_alice_agent.adapters.storage import InMemoryStorageAdapter

        storage = InMemoryStorageAdapter()
        reaction = {axis: 0.08 for axis in (
            "valence", "arousal", "dominance", "energy", "stress", "loneliness"
        )}
        reaction["importance"] = 0.4
        llm = ReactionLLM(json.dumps(reaction))
        service = DailyEventService(
            storage, StateManager(storage, StateReducer()), llm, ContextBuilder(),
            DecisionService(LowConfidenceJev(), mode=DecisionMode.PRIMARY),
            enabled=True,
        )
        scores, importance, source = await service._reaction(
            {"category": "routine"}, "Alice整理了手边的东西。",
            AgentState(), "trace", session_id="session",
        )
        self.assertEqual(source, "jev_llm_blend")
        self.assertAlmostEqual(scores["valence"], 0.005)
        self.assertAlmostEqual(importance, 0.25)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.last_request.stage, "daily_event_reaction")

    async def test_invalid_or_disabled_llm_daily_score_keeps_bounded_rule(self) -> None:
        from astrbot_plugin_alice_agent.adapters.storage import InMemoryStorageAdapter

        for llm_enabled, reaction in ((True, "not json"), (False, "{}")):
            storage = InMemoryStorageAdapter()
            llm = ReactionLLM(reaction)
            service = DailyEventService(
                storage, StateManager(storage, StateReducer()), llm, ContextBuilder(),
                DecisionService(LowConfidenceJev(), mode=DecisionMode.PRIMARY),
                enabled=True, llm_enabled=llm_enabled,
            )
            scores, _importance, source = await service._reaction(
                {"category": "routine"}, "Alice整理了手边的东西。",
                AgentState(), "trace",
            )
            self.assertEqual(source, "bounded_rule_low_confidence_jev")
            self.assertEqual(scores["valence"], 0.02)
            self.assertEqual(llm.calls, int(llm_enabled))

    async def test_unbounded_or_unrelated_llm_story_falls_back_to_seed(self) -> None:
        from astrbot_plugin_alice_agent.alice_agent.daily_events.service import _TEMPLATES

        fallback = _TEMPLATES["routine"][0]
        self.assertEqual(
            DailyEventService._safe_text("Alice突然拯救了世界。", fallback, "routine"),
            fallback,
        )
        self.assertEqual(
            DailyEventService._safe_text("Alice整理了桌上的东西。", fallback, "routine"),
            "Alice整理了桌上的东西。",
        )

    async def test_batch_and_mood_are_idempotent_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mood.db"
            first_storage = SQLiteStorageAdapter(path)
            first_states = StateManager(first_storage, StateReducer())
            llm = EventLLM()
            service = DailyEventService(
                first_storage, first_states, llm, ContextBuilder(),
                DecisionService(None), enabled=True, max_events=3, jev_enabled=False,
            )
            day = datetime(2026, 9, 28, 12)
            session_id = next(
                f"session-{index}" for index in range(100)
                if service._count(service._rng(f"session-{index}", day.date().isoformat()), 3) > 0
            )
            events = await service.ensure(session_id, "user", day.timestamp())
            self.assertGreater(len(events), 0)
            self.assertEqual(llm.calls, len(events))
            self.assertTrue(all(item["stage"] == 1 for item in events))
            state = await first_states.get(session_id)
            self.assertEqual(state.daily_event_day, day.date().isoformat())
            self.assertEqual(state.daily_events, [item["text"] for item in events])

            second_storage = SQLiteStorageAdapter(path)
            second_states = StateManager(second_storage, StateReducer())
            second_llm = EventLLM()
            restarted = DailyEventService(
                second_storage, second_states, second_llm, ContextBuilder(),
                DecisionService(None), enabled=True, max_events=3, jev_enabled=False,
            )
            self.assertEqual(await restarted.ensure(session_id, "user", day.timestamp()), events)
            self.assertEqual(second_llm.calls, 0)
            self.assertEqual(await second_states.get(session_id), state)

            no_events = DailyEventService(
                second_storage, second_states, second_llm, ContextBuilder(),
                DecisionService(None), enabled=True, max_events=0, jev_enabled=False,
            )
            tomorrow = day + timedelta(days=1)
            self.assertEqual(await no_events.ensure(session_id, "user", tomorrow.timestamp()), [])
            self.assertEqual((await second_states.get(session_id)).daily_event_day, tomorrow.date().isoformat())
            self.assertEqual(second_llm.calls, 0)
            await first_states.close()
            await second_states.close()

    async def test_jev_primary_is_bounded_and_shadow_keeps_rule_reaction(self) -> None:
        from astrbot_plugin_alice_agent.adapters.storage import InMemoryStorageAdapter

        seed = {"category": "routine"}
        for mode, expected_source, expected_valence in (
            (DecisionMode.PRIMARY, "jev_bounded", -0.12),
            (DecisionMode.SHADOW, "bounded_rule_shadow_jev", 0.02),
        ):
            storage = InMemoryStorageAdapter()
            service = DailyEventService(
                storage, StateManager(storage, StateReducer()), EventLLM(), ContextBuilder(),
                DecisionService(NegativeJev(), mode=mode), enabled=True,
            )
            reaction, _importance, source = await service._reaction(
                seed, "Alice整理了手边的东西。", AgentState(), "trace"
            )
            self.assertEqual(source, expected_source)
            self.assertAlmostEqual(reaction["valence"], expected_valence)

    async def test_partial_jev_reaction_falls_back_to_rule(self) -> None:
        from astrbot_plugin_alice_agent.adapters.storage import InMemoryStorageAdapter

        storage = InMemoryStorageAdapter()
        service = DailyEventService(
            storage, StateManager(storage, StateReducer()), EventLLM(), ContextBuilder(),
            DecisionService(PartialJev(), mode=DecisionMode.PRIMARY), enabled=True,
        )
        reaction, _importance, source = await service._reaction(
            {"category": "routine"}, "Alice整理了手边的东西。", AgentState(), "trace"
        )
        # DecisionService rejects partial answer sets before DailyEventService sees them.
        self.assertEqual(source, "bounded_rule")
        self.assertAlmostEqual(reaction["valence"], 0.02)


if __name__ == "__main__":
    unittest.main()
