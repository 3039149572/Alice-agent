from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import datetime

from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.proactive.diagnostics import format_status, next_window_open
from astrbot_plugin_alice_agent.alice_agent.proactive.policies import LocalTimeWindow
from astrbot_plugin_alice_agent.tests.test_autonomy import FakeClock, build_loop


class ProactiveDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_does_not_register_generate_draw_claim_or_send(self):
        now = datetime(2026, 10, 21, 12).timestamp()
        loop, store, planner, sender = build_loop(FakeClock(now), active_window=900)
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", now - 60, "private content"))
        before = copy.deepcopy((store.sessions, store.drives, store.actions, store.daily_decisions, store.sends))
        for _ in range(3):
            status = await loop.inspect_session("s", "u")
            self.assertTrue(status["read_only"])
            self.assertIn("active_conversation", status["blockers"])
            self.assertIsNone(status["daily_probability"])
        self.assertEqual(before, (store.sessions, store.drives, store.actions, store.daily_decisions, store.sends))
        self.assertEqual(planner.calls, 0)
        self.assertNotIn("private content", format_status(status))
        self.assertEqual((await loop.inspect_session("s", "different-user"))["blockers"][-1], "session_unregistered")

    async def test_cached_silence_and_unanswered_have_distinct_reasons(self):
        now = datetime(2026, 10, 21, 12).timestamp()
        loop, store, planner, sender = build_loop(FakeClock(now))
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", now - 3600, "hello"))
        day = datetime.fromtimestamp(now).date().isoformat()
        saved = {"eligible": False, "probability": 0.4, "roll": 0.9, "source": "rule"}
        await store.save_daily_proactive_decision("s", day, saved)
        await store.record_send("u", "s", "topic", now - 1800)
        status = await loop.inspect_session("s", "u")
        self.assertIn("probability_silence", status["blockers"])
        self.assertIn("unanswered", status["blockers"])
        self.assertEqual(await store.get_daily_proactive_decision("s", day), saved)
        self.assertTrue(any("下一本地日期" in text for text in status["next_conditions"]))

    async def test_overnight_window_and_next_local_opening(self):
        now = datetime(2026, 10, 21, 12).timestamp()
        window = LocalTimeWindow("21:00", "06:00")
        self.assertEqual(datetime.fromtimestamp(next_window_open(window, now)).hour, 21)
        inside = datetime(2026, 10, 21, 2).timestamp()
        self.assertEqual(next_window_open(window, inside), inside)

    async def test_command_is_self_scoped_and_bypasses_chat_filter(self):
        from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin
        from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
        from astrbot_plugin_alice_agent.adapters.astrbot.message_filter import AlicePrivateMessageFilter
        plugin = _make_plugin(self, lambda _event: self.fail("status must not generate a chat reply"))
        event = FakeEvent("/alice-status")
        self.assertFalse(AlicePrivateMessageFilter().filter(event, {"wake_prefix": []}))
        self.assertTrue(AlicePrivateMessageFilter().filter(FakeEvent("/"), {"wake_prefix": []}))
        result = [item async for item in plugin.alice_status(event)]
        self.assertIn("只读", result[0][1])
        self.assertTrue(event.stopped)
