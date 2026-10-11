from __future__ import annotations

import unittest

from astrbot_plugin_alice_agent.alice_agent.planner.parser import ActionPlanParser


class ActionPlanParserTests(unittest.TestCase):
    def test_invalid_json_uses_reply_fallback(self) -> None:
        self.assertEqual(ActionPlanParser().parse("broken").action, "reply")

    def test_unknown_action_and_memory_ids_are_sanitized(self) -> None:
        raw = '{"action":"launch_missile","energy":4,"memory_ids":["valid","fake"]}'
        plan = ActionPlanParser().parse(raw, {"valid"})
        self.assertEqual(plan.action, "reply")
        self.assertEqual(plan.energy, 1.0)
        self.assertEqual(plan.memory_ids, ["valid"])


if __name__ == "__main__":
    unittest.main()
