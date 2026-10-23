from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.storage.sqlite import SQLiteStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.decision.models import DecisionMode
from astrbot_plugin_alice_agent.alice_agent.decision.schemas import planner_questions
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.decision.evaluation import evaluate_predictions
from astrbot_plugin_alice_agent.alice_agent.decision.metrics import DecisionMetrics
from astrbot_plugin_alice_agent.alice_agent.perception.decision_service import DecisionAwarePerceptionService
from astrbot_plugin_alice_agent.tests.test_decision_layer import MockDecisionModel
from astrbot_plugin_alice_agent.tests.test_vad_comparison import FixedJev, FixedVAD, ExistingPerception, event


class DecisionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_modes_are_independent_and_off_makes_no_request(self):
        model = MockDecisionModel()
        service = DecisionService(model, mode=DecisionMode.OFF, task_policies={
            "planner": {"mode": "hybrid", "auto_threshold": .98}, "memory": {"mode": "off"},
        })
        planner, memory = service.for_task("planner"), service.for_task("memory")
        self.assertTrue(planner.enabled)
        self.assertFalse(memory.enabled)
        result = await planner.decide({}, planner_questions([]), decision_type="planner", schema_version="v1", trace_id="t")
        self.assertEqual(result.mode, "hybrid")
        self.assertFalse(planner.policy.may_use_model_decision(planner.mode, .95))
        self.assertIsNone(await memory.decide({}, {}, decision_type="memory", schema_version="v1", trace_id="t"))
        self.assertEqual(model.calls, 1)
        await service.close()

    async def test_shadow_can_observe_opposite_jev_affect_without_intervention(self):
        values = []
        for valence in (0, 4):
            service = DecisionService(FixedJev(valence=valence, arousal=4, dominance=4, label="sad"), mode=DecisionMode.SHADOW)
            perception = await DecisionAwarePerceptionService(
                ExistingPerception(), service.for_task("perception"), vad_model=FixedVAD(),
                affect_intervention_enabled=False,
            ).analyze(event(), [])
            values.append(perception.prompt_values())
            self.assertFalse(perception.emotion_comparison["jev_affect_intervention_enabled"])
            self.assertIsNotNone(perception.emotion_comparison["score_sets"]["jev"])
            await service.close()
        self.assertEqual(values[0], values[1])

    async def test_grouped_metrics_persist_with_histogram_and_cache_is_not_a_call(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metrics.db"
            service = DecisionService(MockDecisionModel(), mode=DecisionMode.SHADOW, metrics_storage=SQLiteStorageAdapter(path))
            for _ in range(2):
                await service.decide({"text": "private"}, planner_questions([]), decision_type="planner", schema_version="v1", trace_id="private-user")
            self.assertEqual(service.metrics.grouped_snapshot()[0]["calls"], 1)
            await service.close()
            restored = DecisionService(None, metrics_storage=SQLiteStorageAdapter(path))
            await restored.start()
            rows = restored.metrics.grouped_snapshot()
            self.assertEqual(rows[0]["calls"], 1)
            self.assertEqual(sum(rows[0]["latency_bins"]), 1)
            self.assertIn("latency_p95_upper_ms", rows[0])
            self.assertNotIn("private", str(rows))
            await restored.close()

    async def test_corrupt_group_does_not_pollute_restored_metrics(self):
        metrics = DecisionMetrics()
        metrics.record_group("planner", "model", "shadow", 40, "ok", {})
        rows = metrics.grouped_snapshot(derived=False)
        bad = copy.deepcopy(rows[0]); bad["latency_bins"][0] = -1
        restored = DecisionMetrics(); restored.restore_groups([bad, rows[0]])
        self.assertEqual(restored.grouped_snapshot()[0]["calls"], 1)

    async def test_eval_reports_coverage_calibration_and_does_not_claim_unreviewed_quality(self):
        labels = [{"id": "a", "task": "planner", "reviewed": True, "expected": {"action": "reply"}},
                  {"id": "b", "task": "planner", "reviewed": True, "expected": {"action": "ignore"}},
                  {"id": "c", "task": "planner", "reviewed": False, "expected": {"action": "reply"}}]
        predictions = [{"id": "a", "task": "planner", "answers": {"action": "reply"}, "confidence": .9, "latency_ms": 100}]
        report = evaluate_predictions(labels, predictions)
        self.assertEqual(report["tasks"]["planner"]["coverage"], .5)
        self.assertEqual(report["tasks"]["planner"]["exact_match_rate_all_selected"], .5)
        self.assertAlmostEqual(report["tasks"]["planner"]["calibration_ece"], .1)
        self.assertFalse(report["quality_claim_supported"])
        self.assertFalse(evaluate_predictions(labels, predictions, include_proposed=True)["quality_claim_supported"])
