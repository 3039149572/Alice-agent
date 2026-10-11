from __future__ import annotations

import json
import unittest

from astrbot_plugin_alice_agent.alice_agent.domain.perception import SOCIAL_SIGNAL_KEYS
from astrbot_plugin_alice_agent.alice_agent.perception.parser import PerceptionParser


class PerceptionParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = PerceptionParser()
        self.valid = {
            "intent": "complain",
            "topic": "attention",
            "user_emotion": "hurt",
            "valence": -0.6,
            "arousal": 0.7,
            "social_signals": {"complaint": 0.8},
            "importance": 0.75,
            "memory_candidate": True,
            "confidence": 0.9,
        }

    def test_valid_json(self) -> None:
        result = self.parser.parse(json.dumps(self.valid))
        self.assertEqual(result.intent, "complain")
        self.assertEqual(result.social_signals["complaint"], 0.8)
        self.assertEqual(set(result.social_signals), set(SOCIAL_SIGNAL_KEYS))
        self.assertIn("valence", result.numeric_evidence)
        self.assertNotIn("dominance", result.numeric_evidence)

    def test_explicit_dominance_is_parsed_and_absent_numbers_are_not_evidence(self) -> None:
        result = self.parser.parse('{"dominance":0.7,"valence":"unknown","confidence":0.8}')
        self.assertEqual(result.dominance, 0.7)
        self.assertEqual(result.valence, 0.0)
        self.assertEqual(result.numeric_evidence, frozenset({"dominance"}))

    def test_invalid_json_falls_back(self) -> None:
        result = self.parser.parse("not-json")
        self.assertEqual(result.intent, "chat")
        self.assertFalse(result.memory_candidate)

    def test_missing_fields_get_defaults(self) -> None:
        result = self.parser.parse('{"topic": "weather"}')
        self.assertEqual(result.topic, "weather")
        self.assertEqual(result.intent, "chat")
        self.assertEqual(result.arousal, 0.2)

    def test_numbers_are_clamped(self) -> None:
        payload = dict(self.valid)
        payload.update({"valence": -9, "arousal": 8, "importance": 2, "confidence": -1})
        payload["social_signals"] = {"conflict": 7, "praise": -2}
        result = self.parser.parse(json.dumps(payload))
        self.assertEqual(result.valence, -1.0)
        self.assertEqual(result.arousal, 1.0)
        self.assertEqual(result.importance, 1.0)
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.social_signals["conflict"], 1.0)
        self.assertEqual(result.social_signals["praise"], 0.0)

    def test_markdown_code_block(self) -> None:
        raw = "```json\n" + json.dumps(self.valid) + "\n```"
        self.assertEqual(self.parser.parse(raw).topic, "attention")

    def test_empty_response_falls_back(self) -> None:
        result = self.parser.parse("")
        self.assertEqual(result.user_emotion, "neutral")


if __name__ == "__main__":
    unittest.main()
