from __future__ import annotations

import json
import unittest

import httpx

from astrbot_plugin_alice_agent.adapters.jev.typesafe import TypesafeJevAdapter
from astrbot_plugin_alice_agent.alice_agent.decision.fallback import DecisionAPIError, DecisionTimeoutError
from astrbot_plugin_alice_agent.alice_agent.decision.models import (
    DecisionAnswer, DecisionMode, DecisionQuestion, DecisionResult,
)
from astrbot_plugin_alice_agent.alice_agent.decision.policies import DecisionEscalationPolicy
from astrbot_plugin_alice_agent.alice_agent.decision.schemas import planner_questions
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryItem, MemoryQuery
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.memory.decision_gate import MemoryDecisionGate
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryRerankConfig, MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.perception.decision_service import DecisionAwarePerceptionService
from astrbot_plugin_alice_agent.alice_agent.planner.decision_service import DecisionAwarePlannerService
from astrbot_plugin_alice_agent.alice_agent.proactive.decision_gate import ProactiveDecisionGate
from astrbot_plugin_alice_agent.alice_agent.proactive.models import CandidateAction, CandidateActionType, UserAvailability
from astrbot_plugin_alice_agent.alice_agent.drives.models import DriveState
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState


class MockDecisionModel:
    def __init__(
        self,
        *,
        confidence: float = 0.95,
        choices: dict[str, str] | None = None,
        nouls: dict[str, float] | None = None,
        scores: dict[str, float] | None = None,
        failure: Exception | None = None,
        partial: bool = False,
    ) -> None:
        self.confidence = confidence
        self.choices = choices or {}
        self.nouls = nouls or {}
        self.scores = scores or {}
        self.failure = failure
        self.partial = partial
        self.calls = 0
        self.last_state = None
        self.last_questions = None

    async def decide(self, state, questions, **kwargs):
        self.calls += 1
        self.last_state = state
        self.last_questions = questions
        if self.failure:
            raise self.failure
        answers = {}
        for key, question in questions.items():
            if question.type == "choice":
                options = list(question.criteria)
                selected = self.choices.get(key, options[0])
                remaining = (1.0 - self.confidence) / max(1, len(options) - 1)
                probabilities = {option: (self.confidence if option == selected else remaining) for option in options}
                answers[key] = DecisionAnswer(
                    "choice", choice=selected, probabilities=probabilities,
                    confidence=self.confidence,
                )
            elif question.type == "score":
                answers[key] = DecisionAnswer(
                    "score", score=self.scores.get(key, 1.0),
                    probabilities={str(i): 1.0 if i == int(self.scores.get(key, 1.0)) else 0.0 for i in range(len(question.criteria))},
                    confidence=self.confidence,
                    legend={str(i): str(value) for i, value in enumerate(question.criteria)},
                )
            else:
                value = self.nouls.get(key, 0.99 if self.confidence >= 0.85 else (0.85 if self.confidence >= 0.6 else 0.55))
                answers[key] = DecisionAnswer("noul", noul=value)
        if self.partial and answers:
            answers.pop(next(iter(answers)))
        return DecisionResult(
            "mock", kwargs["trace_id"], kwargs["decision_type"], kwargs["schema_version"],
            "", "mock-jev", 1.0, answers=answers,
            usage={"input_tokens": 10, "output_tokens": 2},
        )

    async def close(self):
        return None


class ExistingPerception:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, event, history):
        self.calls += 1
        return PerceptionResult(intent="ask", confidence=0.9)


class ExistingPlanner:
    def __init__(self, action: str = "reply") -> None:
        self.action = action
        self.calls = 0

    async def plan(self, *args, **kwargs):
        self.calls += 1
        return ActionPlan(action=self.action, source="llm")


def event() -> AgentEvent:
    return AgentEvent("e", "trace", "USER_MESSAGE", "test", "session", "user", 1.0, "你今天怎么一直不理我。")


class DecisionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_medium_low_confidence_metrics(self) -> None:
        question = {"route": DecisionQuestion("choice", "route", {"a": None, "b": None})}
        for confidence, level in ((0.95, "high"), (0.82, "medium"), (0.70, "low")):
            model = MockDecisionModel(confidence=confidence)
            service = DecisionService(
                model, mode=DecisionMode.HYBRID,
                policy=DecisionEscalationPolicy(0.85, 0.60), cache_ttl_seconds=0,
            )
            result = await service.decide({}, question, decision_type="planner", schema_version="test_v1", trace_id="t")
            self.assertIsNotNone(result)
            self.assertEqual(getattr(service.metrics.bucket("planner"), f"{level}_confidence"), 1)

    async def test_timeout_api_error_and_partial_answers_fallback(self) -> None:
        question = {"x": DecisionQuestion("noul", "x")}
        cases = (
            MockDecisionModel(failure=DecisionTimeoutError("timeout")),
            MockDecisionModel(failure=DecisionAPIError("api")),
            MockDecisionModel(partial=True),
        )
        for model in cases:
            service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
            self.assertIsNone(await service.decide({}, question, decision_type="perception", schema_version="v1", trace_id="t"))
            self.assertEqual(service.metrics.bucket("perception").fallbacks, 1)
            self.assertEqual(service.metrics.snapshot()["perception"]["fallback_rate"], 1.0)

    async def test_cache_key_includes_schema_and_returns_new_trace(self) -> None:
        model = MockDecisionModel()
        service = DecisionService(model, mode=DecisionMode.SHADOW, cache_ttl_seconds=60)
        question = {"x": DecisionQuestion("noul", "x")}
        first = await service.decide({"a": 1}, question, decision_type="memory", schema_version="v1", trace_id="t1")
        second = await service.decide({"a": 1}, question, decision_type="memory", schema_version="v1", trace_id="t2")
        third = await service.decide({"a": 1}, question, decision_type="memory", schema_version="v2", trace_id="t3")
        self.assertEqual(model.calls, 2)
        self.assertTrue(second.cached)
        self.assertEqual(second.trace_id, "t2")
        self.assertNotEqual(first.decision_id, second.decision_id)
        self.assertFalse(third.cached)


class DecisionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_weak_planner_number_blends_without_replacing_high_confidence_action(self) -> None:
        class WeakDirectnessModel(MockDecisionModel):
            async def decide(self, state, questions, **kwargs):
                result = await super().decide(state, questions, **kwargs)
                result.answers["directness"].confidence = 0.30
                return result

        model = WeakDirectnessModel(
            confidence=0.98, choices={"action": "comfort"},
            nouls={
                "requires_deep_reasoning": 0.01,
                "use_memory": 0.01,
                "ask_followup": 0.01,
            },
            scores={"directness": 2.0},
        )
        service = DecisionService(model, mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        existing = ExistingPlanner("reply")
        planner = DecisionAwarePlannerService(existing, service)
        plan = await planner.plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(plan.action, "comfort")
        self.assertEqual(plan.source, "jev")
        self.assertAlmostEqual(plan.directness, 0.59375)
        self.assertEqual(existing.calls, 1)
        comparison = planner.pop_comparison(event().trace_id)
        self.assertEqual(comparison["numeric_decision_source"], "jev_llm_blend")
        self.assertEqual(comparison["selected_decision_source"], "jev")

    async def test_perception_maps_bounded_jev_result(self) -> None:
        model = MockDecisionModel(
            confidence=0.98,
            choices={"intent": "complain", "emotion": "annoyed", "relationship_event": "relationship_negative"},
            nouls={"social_complaint": 0.95, "memory_worthy": 0.9, "requires_deep_reasoning": 0.01},
            scores={"importance": 3.0},
        )
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        existing = ExistingPerception()
        perception = await DecisionAwarePerceptionService(existing, service).analyze(event(), [])
        self.assertEqual(perception.intent, "complain")
        self.assertEqual(perception.user_emotion, "neutral")
        self.assertGreater(perception.social_signals["complaint"], 0.9)
        self.assertAlmostEqual(perception.importance, 0.75)
        self.assertEqual(existing.calls, 1)

    async def test_planner_high_confidence_avoids_llm(self) -> None:
        model = MockDecisionModel(
            confidence=0.98,
            choices={"action": "comfort", "tone": "gentle", "response_length": "short"},
            nouls={"requires_deep_reasoning": 0.01, "use_memory": 0.01, "ask_followup": 0.01},
        )
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        existing = ExistingPlanner()
        plan = await DecisionAwarePlannerService(existing, service).plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(plan.action, "comfort")
        self.assertEqual(plan.source, "jev")
        self.assertEqual(existing.calls, 0)
        self.assertEqual(service.metrics.bucket("planner").llm_calls_avoided, 1)

    async def test_planner_medium_confidence_escalates_llm(self) -> None:
        model = MockDecisionModel(
            confidence=0.70,
            choices={"action": "comfort", "tone": "gentle", "response_length": "short"},
            nouls={"requires_deep_reasoning": 0.15},
        )
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        existing = ExistingPlanner("clarify")
        plan = await DecisionAwarePlannerService(existing, service).plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(plan.action, "clarify")
        self.assertEqual(existing.calls, 1)
        self.assertEqual(service.metrics.bucket("planner").llm_escalations, 1)

    async def test_shadow_records_disagreement_but_uses_existing(self) -> None:
        model = MockDecisionModel(
            confidence=0.98,
            choices={"action": "comfort", "tone": "gentle", "response_length": "short"},
            nouls={"requires_deep_reasoning": 0.01},
        )
        service = DecisionService(model, mode=DecisionMode.SHADOW, cache_ttl_seconds=0)
        existing = ExistingPlanner("reply")
        planner = DecisionAwarePlannerService(existing, service)
        plan = await planner.plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(plan.action, "reply")
        comparison = planner.pop_comparison(event().trace_id)
        self.assertEqual(comparison["jev"]["action"], "comfort")
        self.assertEqual(comparison["llm"]["action"], "reply")
        self.assertEqual(comparison["selected_decision_source"], "llm")
        self.assertIsNone(planner.pop_comparison(event().trace_id))
        self.assertEqual(existing.calls, 1)
        self.assertEqual(service.metrics.bucket("planner").existing_disagreements, 1)
        snapshot = service.metrics.snapshot()["planner"]
        self.assertEqual(snapshot["existing_comparisons"], 1)
        self.assertEqual(snapshot["disagreement_rate"], 1.0)

    async def test_primary_falls_back_below_eighty_and_accepts_eighty(self) -> None:
        medium_model = MockDecisionModel(
            confidence=0.70,
            choices={"action": "comfort", "tone": "gentle", "response_length": "short"},
            nouls={"requires_deep_reasoning": 0.15},
            scores={"directness": 2.0},
        )
        medium_service = DecisionService(medium_model, mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        medium_existing = ExistingPlanner("reply")
        medium_plan = await DecisionAwarePlannerService(medium_existing, medium_service).plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(medium_plan.action, "reply")
        self.assertEqual(medium_plan.source, "llm")
        self.assertGreater(medium_plan.directness, 0.5)
        self.assertEqual(medium_existing.calls, 1)

        accepted_model = MockDecisionModel(
            confidence=0.80,
            choices={"action": "comfort", "tone": "gentle"},
            nouls={
                "requires_deep_reasoning": 0.1,
                "use_memory": 0.1,
                "ask_followup": 0.1,
            },
        )
        accepted_existing = ExistingPlanner("reply")
        accepted = await DecisionAwarePlannerService(
            accepted_existing,
            DecisionService(accepted_model, mode=DecisionMode.PRIMARY, cache_ttl_seconds=0),
        ).plan(event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user"))
        self.assertEqual(accepted.action, "comfort")
        self.assertEqual(accepted_existing.calls, 0)

        low_model = MockDecisionModel(
            confidence=0.40,
            choices={"action": "comfort", "tone": "gentle", "response_length": "short"},
            nouls={"requires_deep_reasoning": 0.45},
        )
        low_service = DecisionService(low_model, mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        low_existing = ExistingPlanner("clarify")
        low_plan = await DecisionAwarePlannerService(low_existing, low_service).plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(low_plan.action, "clarify")
        self.assertEqual(low_existing.calls, 1)

    async def test_off_mode_never_calls_decision_model(self) -> None:
        model = MockDecisionModel()
        service = DecisionService(model, mode=DecisionMode.OFF, cache_ttl_seconds=0)
        existing = ExistingPlanner("reply")
        plan = await DecisionAwarePlannerService(existing, service).plan(
            event(), PerceptionResult(), AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(plan.action, "reply")
        self.assertEqual(model.calls, 0)
        self.assertEqual(existing.calls, 1)

    async def test_reactive_failure_uses_existing_perception_and_planner(self) -> None:
        model = MockDecisionModel(failure=DecisionAPIError("offline"))
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        perception_existing = ExistingPerception()
        perception = await DecisionAwarePerceptionService(perception_existing, service).analyze(event(), [])
        planner_existing = ExistingPlanner("reply")
        plan = await DecisionAwarePlannerService(planner_existing, service).plan(
            event(), perception, AgentState(), [], [], RelationshipState("user")
        )
        self.assertEqual(perception.intent, "ask")
        self.assertEqual(plan.action, "reply")
        self.assertEqual(perception_existing.calls, 1)
        self.assertEqual(planner_existing.calls, 1)

    async def test_proactive_gate_send_and_failure_policy(self) -> None:
        model = MockDecisionModel(
            confidence=0.98,
            choices={"action": "send"},
            nouls={
                "natural_reason_exists": 0.99,
                "likely_to_interrupt": 0.01,
                "relevant_context_exists": 0.99,
                "requires_deep_reasoning": 0.01,
            },
            scores={"appropriateness": 4.0},
        )
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        gate = ProactiveDecisionGate(service)
        decision = await gate.evaluate(
            CandidateAction(CandidateActionType.MEMORY_FOLLOW_UP, "exam", relevance=0.9),
            DriveState("s", "u", social_need=0.8, curiosity=0.9),
            RelationshipState("u", familiarity=0.8, trust=0.8, closeness=0.8),
            [], UserAvailability(manual_status="available"), "trace",
        )
        self.assertEqual(decision.action, "send")

        failed_service = DecisionService(
            MockDecisionModel(failure=DecisionAPIError("offline")),
            mode=DecisionMode.HYBRID, cache_ttl_seconds=0,
        )
        failed = await ProactiveDecisionGate(failed_service).evaluate(
            CandidateAction(CandidateActionType.MEMORY_FOLLOW_UP, "exam", relevance=0.9),
            DriveState("s", "u"), RelationshipState("u"), [], UserAvailability(), "trace",
        )
        self.assertEqual(failed.action, "do_nothing")

    async def test_memory_candidates_use_one_call_and_code_reranks(self) -> None:
        model = MockDecisionModel(
            confidence=0.98,
            nouls={"memory_0_relevant": 0.01, "memory_1_relevant": 0.99},
        )
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        gate = MemoryDecisionGate(service)
        items = [
            MemoryItem("a", "unhelpful", retrieval_score=0.8, importance=0.5),
            MemoryItem("b", "exam context", retrieval_score=0.8, importance=0.5),
        ]
        await gate.evaluate(event(), PerceptionResult(topic="exam"), items)
        query = MemoryQuery("session", "user", "考完了", "exam", PerceptionResult(), AgentState())
        ranked = MemoryReranker(MemoryRerankConfig(jev_relevance_blend=0.5)).rerank(items, 2, query=query)
        self.assertEqual(model.calls, 1)
        self.assertEqual([item.id for item in ranked.selected], ["b", "a"])


class TypesafeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_schema_matches_official_contract(self) -> None:
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization")
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={
                "model": "jev-1.13.0",
                "answers": {
                    "intent": {
                        "type": "choice", "choice": "ask",
                        "probabilities": {"ask": 0.9, "other": 0.1}, "confidence": 0.8,
                    }
                },
                "usage": {"input_tokens": 12, "output_tokens": 3},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = TypesafeJevAdapter(
            endpoint="https://api.typesafe.ai/v1/systemone",
            api_key="secret-test-key", model="jev-latest", client=client,
        )
        result = await adapter.decide(
            {"message": "hello"},
            {"intent": DecisionQuestion("choice", "intent", {"ask": None, "other": None})},
            decision_type="perception", schema_version="v1", trace_id="t",
        )
        self.assertEqual(captured["authorization"], "Bearer secret-test-key")
        self.assertEqual(set(captured["payload"]), {"state", "model", "questions"})
        self.assertEqual(captured["payload"]["questions"]["intent"]["criteria"], {"ask": None, "other": None})
        self.assertEqual(result.model, "jev-1.13.0")
        await client.aclose()

    async def test_unknown_model_http_error_is_safely_fallback(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "unknown model"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = TypesafeJevAdapter(
            endpoint="https://api.typesafe.ai/v1/systemone",
            api_key="secret", model="unknown-model", client=client,
        )
        service = DecisionService(adapter, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        result = await service.decide(
            "x", {"x": DecisionQuestion("noul", "x")},
            decision_type="perception", schema_version="v1", trace_id="t",
        )
        self.assertIsNone(result)
        self.assertEqual(service.metrics.bucket("perception").fallbacks, 1)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
