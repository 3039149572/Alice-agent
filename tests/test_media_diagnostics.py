from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_alice_agent.adapters.diagnostics import AtomicJSONTurnLogger
from astrbot_plugin_alice_agent.alice_agent.core.pipeline import AgentPipeline


class MediaDiagnosticsTests(unittest.TestCase):
    def test_emoji_alignment_summary_is_bounded_and_json_serializable(self) -> None:
        media = SimpleNamespace(
            send_voice=False,
            send_emoji=True,
            source="deterministic",
            reason="media selected",
            voice=None,
            emoji=SimpleNamespace(
                id="emoji_0", kind="emoji", description="庆祝", path="private/local/path.png"
            ),
            diagnostics={
                "emoji_alignment": {
                    "basis": "response_only",
                    "response_intents": ["celebration", "farewell"],
                    "candidate_count": 99,
                    "eligible_count": 1,
                    "abstained": False,
                    "candidates": [
                        {
                            "id": "emoji_0",
                            "eligible": True,
                            "reason": "intent_match",
                            "intent": "celebration",
                            "score": 0.82,
                            "path": "private/local/path.png",
                            "description": "用户说了需要保存的私聊原文",
                        }
                    ] + [
                        {
                            "id": f"emoji_{index}",
                            "eligible": False,
                            "reason": "unrelated_intent",
                            "intent": "crying",
                            "score": 0.0,
                        }
                        for index in range(20)
                    ],
                    "user_text": "不得写入诊断的私聊原文",
                },
                "unrecognized": "ignored",
            },
        )

        diagnostics = AgentPipeline._media_diagnostics(media)
        encoded = json.dumps(diagnostics, ensure_ascii=False, allow_nan=False)
        saved_shape = json.loads(encoded)

        alignment = saved_shape["diagnostics"]["emoji_alignment"]
        self.assertEqual(alignment["basis"], "response_only")
        self.assertEqual(alignment["candidate_count"], 99)
        self.assertEqual(alignment["eligible_count"], 1)
        self.assertEqual(len(alignment["candidates"]), 12)
        self.assertEqual(alignment["candidates"][0], {
            "id": "emoji_0",
            "eligible": True,
            "reason": "intent_match",
            "intent": "celebration",
            "score": 0.82,
        })
        self.assertEqual(saved_shape["emoji"]["id"], "emoji_0")
        self.assertNotIn("path", saved_shape["emoji"])
        self.assertNotIn("user_text", encoded)
        self.assertNotIn("不得写入诊断的私聊原文", encoded)
        self.assertNotIn("用户说了需要保存的私聊原文", encoded)
        self.assertNotIn("unrecognized", saved_shape["diagnostics"])

    def test_media_summary_is_written_to_turn_record_and_disabled_logger_writes_nothing(self) -> None:
        media = SimpleNamespace(
            send_emoji=False,
            diagnostics={
                "emoji_alignment": {
                    "basis": "response_only",
                    "response_intents": ["question"],
                    "candidate_count": 1,
                    "eligible_count": 0,
                    "abstained": True,
                    "candidates": [
                        {
                            "id": "emoji_0",
                            "eligible": False,
                            "reason": "unrelated_intent",
                            "intent": "crying",
                            "score": 0.0,
                        }
                    ],
                }
            },
        )
        summary = AgentPipeline._media_diagnostics(media)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "test_turns"
            logger = AtomicJSONTurnLogger(directory)
            path = logger.write({
                "trace_id": "media-diagnostics",
                "turn_diagnostics": {"media_decision": summary},
            })
            self.assertIsNotNone(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["turn_diagnostics"]["media_decision"]["diagnostics"]["emoji_alignment"]["abstained"],
                True,
            )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "test_turns"
            logger = AtomicJSONTurnLogger(directory)
            path = logger.write(
                {"trace_id": "disabled", "turn_diagnostics": {"media_decision": summary}},
                enabled=lambda: False,
            )
            self.assertIsNone(path)
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
