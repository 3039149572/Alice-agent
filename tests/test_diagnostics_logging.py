from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.diagnostics import AtomicJSONTurnLogger


class AtomicJSONTurnLoggerTests(unittest.TestCase):
    def test_writes_one_complete_json_and_removes_sensitive_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "turns"
            logger = AtomicJSONTurnLogger(directory)
            path = logger.write({
                "trace_id": "trace-1",
                "user_text": "hello",
                "jev_api_key": "must-not-persist",
                "decision": {"confidence": 0.9, "input_tokens": 12},
                "numeric_evidence": frozenset({"valence"}),
            })

            self.assertIsNotNone(path)
            self.assertEqual(path.suffix, ".json")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["user_text"], "hello")
            self.assertNotIn("jev_api_key", saved)
            self.assertEqual(saved["decision"]["input_tokens"], 12)
            self.assertEqual(saved["numeric_evidence"], ["valence"])
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_switch_turning_off_before_atomic_replace_discards_pending_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "turns"
            logger = AtomicJSONTurnLogger(directory)
            checks = 0

            def enabled() -> bool:
                nonlocal checks
                checks += 1
                return checks == 1

            path = logger.write({"trace_id": "trace-2", "text": "pending"}, enabled=enabled)

            self.assertIsNone(path)
            self.assertEqual(list(directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
