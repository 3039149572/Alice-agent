from __future__ import annotations

import json
import unittest

from astrbot_plugin_alice_agent.alice_agent.decision.models import (
    DecisionAnswer, DecisionMode, DecisionResult,
)
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.perception.decision_service import (
    DecisionAwarePerceptionService,
)
from astrbot_plugin_alice_agent.alice_agent.perception.vad import VADModelScore


class ExistingPerception:
    def __init__(self, result: PerceptionResult | None = None) -> None:
        self.result = result or PerceptionResult(
            intent="ask", user_emotion="positive", valence=0.6,
            arousal=0.3, dominance=0.4, confidence=0.8,
        )
        self.calls = 0

    async def analyze(self, event, history):
        self.calls += 1
        return PerceptionResult(
            intent=self.result.intent,
            topic=self.result.topic,
            user_emotion=self.result.user_emotion,
            valence=self.result.valence,
            arousal=self.result.arousal,
            dominance=self.result.dominance,
            social_signals=dict(self.result.social_signals),
            importance=self.result.importance,
            memory_candidate=self.result.memory_candidate,
            confidence=self.result.confidence,
            numeric_evidence=self.result.numeric_evidence,
        )


class FailingPerception:
    async def analyze(self, event, history):
        raise TimeoutError("LLM unavailable")


class FixedVAD:
    def __init__(self, vector=(5.0, 1.0, 3.0), *, failure: Exception | None = None):
        self.vector = vector
        self.failure = failure
        self.calls = 0

    async def score(self, text):
        self.calls += 1
        if self.failure:
            raise self.failure
        raw = tuple(self.vector)
        return VADModelScore(
            raw=raw,
            normalized=tuple((item - 1.0) / 4.0 for item in raw),
            reported_axes={},
        )

    async def close(self):
        return None


class FixedJev:
    def __init__(
        self, *, valence: float, arousal: float, dominance: float,
        label: str, confidence: float = 0.98,
        score_confidences: dict[str, float] | None = None,
    ):
        self.scores = {
            "vad_valence": valence,
            "vad_arousal": arousal,
            "vad_dominance": dominance,
            "importance": 2.0,
            "emotion_intensity": 3.0,
            "warmth": 2.0,
            "tension": 1.0,
            "suppression": 0.0,
        }
        self.label = label
        self.confidence = confidence
        self.score_confidences = score_confidences or {}
        self.calls = 0

    async def decide(self, state, questions, **kwargs):
        self.calls += 1
        answers = {}
        for key, question in questions.items():
            if question.type == "choice":
                options = list(question.criteria)
                selected = self.label if key == "emotion" else options[0]
                answers[key] = DecisionAnswer(
                    "choice", choice=selected,
                    probabilities={option: 1.0 if option == selected else 0.0 for option in options},
                    confidence=self.confidence,
                )
            elif question.type == "score":
                score = self.scores.get(key, 1.0)
                answers[key] = DecisionAnswer(
                    "score", score=score,
                    probabilities={str(index): 1.0 if index == int(score) else 0.0 for index in range(len(question.criteria))},
                    confidence=self.score_confidences.get(key, self.confidence),
                    legend={str(index): str(value) for index, value in enumerate(question.criteria)},
                )
            else:
                answers[key] = DecisionAnswer(
                    "noul", noul=0.01 if key == "requires_deep_reasoning" else 0.9,
                    confidence=self.score_confidences.get(key, self.confidence),
                )
        return DecisionResult(
            "fixed", kwargs["trace_id"], kwargs["decision_type"],
            kwargs["schema_version"], "", "jev-test", 123.0,
            answers=answers,
        )

    async def close(self):
        return None


def event() -> AgentEvent:
    return AgentEvent(
        event_id="event-1", trace_id="trace-1", type="user_message",
        source="test", session_id="session-1", user_id="user-1",
        timestamp=1.0, text="今天真开心",
    )


class VADComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_keeps_both_jev_and_llm_perception_decisions(self) -> None:
        decisions = DecisionService(
            FixedJev(valence=0.0, arousal=4.0, dominance=4.0, label="sad"),
            mode=DecisionMode.SHADOW,
        )
        perception = await DecisionAwarePerceptionService(
            ExistingPerception(), decisions, vad_model=FixedVAD(),
        ).analyze(event(), [])
        comparison = perception.emotion_comparison["decision_comparison"]
        self.assertEqual(comparison["jev"]["user_emotion"], "sad")
        self.assertEqual(comparison["llm"]["user_emotion"], "positive")
        self.assertIn("answers", comparison["jev_raw"])
        self.assertEqual(comparison["selected_decision_source"], "llm")
        self.assertEqual(comparison["affect_source"], "vad_jev_blend")

    async def test_jev_and_local_vad_blend_is_the_selected_affect(self) -> None:
        selected = []
        prompts = []
        comparisons = []
        for jev in (
            FixedJev(valence=0.0, arousal=4.0, dominance=4.0, label="sad"),
            FixedJev(valence=4.0, arousal=0.0, dominance=0.0, label="excited"),
        ):
            existing = ExistingPerception()
            decisions = DecisionService(jev, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
            perception = await DecisionAwarePerceptionService(
                existing, decisions, vad_model=FixedVAD(), jev_weight=0.5,
            ).analyze(event(), [])
            selected.append(perception.prompt_values())
            _, prompt = ContextBuilder().for_response(
                event(), perception, AgentState(), [], ActionPlan(), [],
            )
            prompts.append(json.loads(prompt)["perception"])
            comparisons.append(perception.emotion_comparison)

        self.assertNotEqual(selected[0], selected[1])
        self.assertNotEqual(prompts[0], prompts[1])
        self.assertEqual(prompts[0]["user_emotion"], "neutral")
        self.assertEqual(prompts[0]["valence"], 0.0)
        self.assertEqual(prompts[0]["arousal"], 0.5)
        self.assertEqual(prompts[0]["dominance"], 0.75)
        self.assertEqual(prompts[1]["valence"], 1.0)
        self.assertEqual(prompts[1]["dominance"], 0.25)
        self.assertNotIn("emotion_comparison", prompts[0])
        self.assertNotIn("score_sets", json.dumps(prompts[0]))
        self.assertEqual(comparisons[0]["score_sets"]["vad_model"], {
            "valence": 1.0, "arousal": 0.0, "dominance": 0.5,
        })
        self.assertEqual(comparisons[0]["score_sets"]["combined"], {
            "valence": 0.5, "arousal": 0.5, "dominance": 0.75,
        })
        self.assertEqual(comparisons[0]["selected_source"], "vad_jev_blend")
        self.assertEqual(comparisons[0]["jev"]["emotion_label"], "sad")
        self.assertEqual(comparisons[0]["jev"]["api_latency_ms"], 123.0)
        self.assertTrue(comparisons[0]["jev"]["answers"])

    async def test_vad_failure_falls_back_to_existing_affect_and_counts_llm(self) -> None:
        existing = ExistingPerception()
        jev = FixedJev(valence=0.0, arousal=4.0, dominance=4.0, label="sad")
        decisions = DecisionService(jev, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        perception = await DecisionAwarePerceptionService(
            existing, decisions,
            vad_model=FixedVAD(failure=TimeoutError("local service timeout")),
        ).analyze(event(), [])

        self.assertEqual(perception.user_emotion, "positive")
        self.assertEqual(perception.valence, 0.6)
        self.assertEqual(perception.emotion_comparison["selected_source"], "existing_perception")
        self.assertEqual(perception.emotion_comparison["vad_model"]["status"], "error")
        self.assertEqual(perception.emotion_comparison["jev"]["scores"]["valence"], 0.0)
        self.assertEqual(decisions.metrics.bucket("perception").llm_calls_avoided, 0)
        self.assertEqual(existing.calls, 1)

    async def test_low_jev_confidence_mixes_llm_numbers_then_local_vad(self) -> None:
        existing = ExistingPerception(PerceptionResult(
            intent="ask", user_emotion="positive", valence=0.6,
            arousal=0.3, dominance=0.4, importance=0.2, confidence=0.8,
        ))
        jev = FixedJev(
            valence=0.0, arousal=4.0, dominance=4.0,
            label="sad", confidence=0.60,
        )
        decisions = DecisionService(jev, mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        result = await DecisionAwarePerceptionService(
            existing, decisions, vad_model=FixedVAD(), jev_weight=0.5,
        ).analyze(event(), [])

        # Jev/LLM numeric weight is 0.5 * (0.60 / 0.80) = 0.375.
        # The result then shares equal weight with the local VAD scores.
        self.assertEqual(existing.calls, 1)
        self.assertAlmostEqual(result.valence, 0.5)
        self.assertAlmostEqual(result.arousal, 0.28125)
        self.assertAlmostEqual(result.dominance, 0.5625)
        self.assertAlmostEqual(result.importance, 0.3125)
        self.assertAlmostEqual(result.social_signals["affection"], 0.3375)
        comparison = result.emotion_comparison
        self.assertEqual(comparison["selected_source"], "vad_jev_llm_blend")
        self.assertEqual(comparison["decision_comparison"]["selected_decision_source"], "llm")
        self.assertAlmostEqual(comparison["score_sets"]["jev_llm"]["valence"], 0.5)
        self.assertAlmostEqual(comparison["numeric_jev_weights"]["valence"], 0.375)

    async def test_missing_llm_numeric_axis_uses_local_vad_for_that_axis(self) -> None:
        existing = ExistingPerception(PerceptionResult(
            valence=0.6, arousal=0.3, dominance=0.4, confidence=0.8,
            numeric_evidence=frozenset({"valence", "arousal"}),
        ))
        decisions = DecisionService(FixedJev(
            valence=0.0, arousal=4.0, dominance=4.0,
            label="sad", confidence=0.60,
        ), mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        result = await DecisionAwarePerceptionService(
            existing, decisions, vad_model=FixedVAD(),
        ).analyze(event(), [])
        self.assertEqual(result.dominance, 0.5)
        self.assertNotIn("dominance", result.emotion_comparison["score_sets"]["jev_llm"])

    async def test_low_axis_confidence_is_mixed_even_when_overall_jev_is_high(self) -> None:
        existing = ExistingPerception()
        decisions = DecisionService(FixedJev(
            valence=0.0, arousal=4.0, dominance=4.0,
            label="sad", score_confidences={"vad_valence": 0.30},
        ), mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        result = await DecisionAwarePerceptionService(
            existing, decisions, vad_model=FixedVAD(),
        ).analyze(event(), [])
        self.assertEqual(existing.calls, 1)
        self.assertEqual(result.emotion_comparison["selected_source"], "vad_jev_llm_blend")
        self.assertAlmostEqual(result.valence, 0.65)
        self.assertAlmostEqual(result.arousal, 0.5)
        self.assertAlmostEqual(result.dominance, 0.75)
        self.assertEqual(set(result.emotion_comparison["score_sets"]["jev_llm"]), {"valence"})

    async def test_zero_confidence_numeric_axes_take_llm_before_local_vad(self) -> None:
        existing = ExistingPerception()
        decisions = DecisionService(FixedJev(
            valence=0.0, arousal=4.0, dominance=4.0,
            label="sad", score_confidences={
                "vad_valence": 0.0, "social_affection": 0.0,
                "importance": 0.0,
            },
        ), mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        result = await DecisionAwarePerceptionService(
            existing, decisions, vad_model=FixedVAD(),
        ).analyze(event(), [])
        self.assertEqual(existing.calls, 1)
        self.assertAlmostEqual(result.valence, 0.8)
        self.assertAlmostEqual(result.social_signals["affection"], 0.0)
        self.assertAlmostEqual(result.importance, 0.2)
        self.assertEqual(result.emotion_comparison["numeric_jev_weights"]["valence"], 0.0)

    async def test_numeric_llm_failure_keeps_valid_local_vad(self) -> None:
        decisions = DecisionService(FixedJev(
            valence=0.0, arousal=4.0, dominance=4.0,
            label="sad", score_confidences={"vad_valence": 0.10},
        ), mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        result = await DecisionAwarePerceptionService(
            FailingPerception(), decisions, vad_model=FixedVAD(),
        ).analyze(event(), [])
        self.assertEqual(result.valence, 1.0)
        self.assertEqual(result.emotion_comparison["llm_error_kind"], "TimeoutError")
        self.assertEqual(result.emotion_comparison["selected_source"], "vad_jev_blend")

    async def test_low_jev_and_failed_local_vad_uses_jev_llm_fusion(self) -> None:
        existing = ExistingPerception()
        decisions = DecisionService(FixedJev(
            valence=0.0, arousal=4.0, dominance=4.0,
            label="sad", confidence=0.60,
        ), mode=DecisionMode.PRIMARY, cache_ttl_seconds=0)
        result = await DecisionAwarePerceptionService(
            existing, decisions,
            vad_model=FixedVAD(failure=TimeoutError("local VAD unavailable")),
        ).analyze(event(), [])
        self.assertEqual(existing.calls, 1)
        self.assertEqual(result.emotion_comparison["selected_source"], "jev_llm_blend")
        self.assertAlmostEqual(result.valence, 0.0)

    async def test_disabled_jev_does_not_call_remote_model_but_vad_still_runs(self) -> None:
        existing = ExistingPerception()
        jev = FixedJev(valence=0.0, arousal=4.0, dominance=4.0, label="sad")
        decisions = DecisionService(None, mode=DecisionMode.OFF)
        perception = await DecisionAwarePerceptionService(
            existing, decisions, vad_model=FixedVAD(),
        ).analyze(event(), [])

        self.assertEqual(jev.calls, 0)
        self.assertEqual(perception.user_emotion, "positive")
        self.assertEqual(perception.emotion_comparison["jev"]["status"], "disabled")
        self.assertEqual(perception.emotion_comparison["score_sets"]["jev"], None)


if __name__ == "__main__":
    unittest.main()
