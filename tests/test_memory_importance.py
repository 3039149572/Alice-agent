from __future__ import annotations

import unittest

from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryItem
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.memory.importance import MemoryImportanceEvaluator
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState


class MemoryImportanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = MemoryImportanceEvaluator()
        self.item = MemoryItem("m", "内容", confidence=0.8)
        self.relationship = RelationshipState("u", closeness=0.5)

    def test_important_plan_scores_above_small_talk(self) -> None:
        small_talk = PerceptionResult(importance=0.1, arousal=0.1, confidence=0.8)
        important_plan = PerceptionResult(
            intent="share_plan", topic="重要考试", importance=0.9,
            arousal=0.7, memory_candidate=True, confidence=0.9,
        )
        low = self.evaluator.evaluate(
            small_talk, AgentState(), self.item, self.relationship,
            new_observation=True,
        )
        high = self.evaluator.evaluate(
            important_plan, AgentState(), self.item, self.relationship,
            new_observation=True,
        )
        self.assertGreater(high.importance, low.importance)

    def test_relationship_event_has_higher_relationship_weight(self) -> None:
        ordinary = PerceptionResult(importance=0.5)
        vulnerable = PerceptionResult(
            importance=0.5,
            social_signals={
                "affection": 0.8, "complaint": 0.0, "teasing": 0.0,
                "conflict": 0.0, "reassurance_seeking": 0.7,
                "vulnerability": 0.9, "praise": 0.0, "concern": 0.0,
            },
        )
        first = self.evaluator.evaluate(
            ordinary, AgentState(), self.item, self.relationship,
            new_observation=True,
        )
        second = self.evaluator.evaluate(
            vulnerable, AgentState(), self.item, self.relationship,
            new_observation=True,
        )
        self.assertGreater(second.relationship_weight, first.relationship_weight)

    def test_all_outputs_are_clamped(self) -> None:
        extreme = PerceptionResult(
            importance=4.0, valence=-3.0, arousal=5.0,
            social_signals={key: 8.0 for key in (
                "affection", "complaint", "teasing", "conflict",
                "reassurance_seeking", "vulnerability", "praise", "concern",
            )},
        )
        result = self.evaluator.evaluate(
            extreme, AgentState(), self.item, self.relationship,
            new_observation=True,
        )
        for value in (
            result.importance, result.emotional_weight,
            result.relationship_weight,
        ):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


if __name__ == "__main__":
    unittest.main()
