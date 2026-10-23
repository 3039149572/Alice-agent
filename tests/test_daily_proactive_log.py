from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_alice_agent.adapters.astrbot.proactive_sender import AstrBotProactiveSender
from astrbot_plugin_alice_agent.adapters.diagnostics import DailyProactiveLogger
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.proactive.daily_policy import DailyProactivePolicy
from astrbot_plugin_alice_agent.tests.test_autonomy import FakeClock, FakeDailyEvents, build_loop
from astrbot_plugin_alice_agent.tests.test_daily_proactive import EVENTS, NOW, Model


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class DailyProactiveLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "proactive_today.log"
        self.clock = FakeClock(NOW)
        self.audit = DailyProactiveLogger(self.path, NullLogger(), clock=self.clock.now)
        self.addAsyncCleanup(self.audit.close)

    async def make_loop(self, roll=0):
        loop, store, planner, sender = build_loop(self.clock)
        loop._audit_sink = self.audit
        loop._daily_events = FakeDailyEvents([dict(item) for item in EVENTS])
        loop._config = replace(loop._config, daily_events_enabled=True)
        model = Model()
        loop._daily_policy = DailyProactivePolicy(store, llm=model, random_value=lambda: roll)
        await loop.on_user_event(AgentEvent("e", "t", "USER_MESSAGE", "test", "s", "u", NOW - 3600, "private user text"))
        return loop, store, planner, sender, model

    async def test_same_day_restart_appends_and_next_day_replaces_only_fixed_file(self):
        await self.audit.record("blocked", reason="active conversation window", session_id="s")
        restored = DailyProactiveLogger(self.path, NullLogger(), clock=self.clock.now)
        await restored.record("planner", reason="今天没有适合分享的内容")
        self.assertEqual(len(read_rows(self.path)), 3)
        unrelated = self.path.with_name("keep.txt")
        unrelated.write_text("keep", encoding="utf-8")
        self.clock.value = datetime(2026, 10, 20, 0, 0, 1).timestamp()
        await restored.record("tick", session_count=0)
        rows = read_rows(self.path)
        self.assertEqual(rows[0]["date"], "2026-10-20")
        self.assertEqual([r.get("stage") for r in rows[1:]], ["tick"])
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertEqual(list(self.path.parent.glob("*.log")), [self.path])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    async def test_rollover_failure_preserves_previous_day_and_next_write_retries(self):
        await self.audit.record("tick")
        previous = self.path.read_bytes()
        self.clock.advance(86400)
        with patch("os.replace", side_effect=PermissionError("file busy")):
            await self.audit.record("tick")
        self.assertEqual(self.path.read_bytes(), previous)
        await self.audit.record("tick")
        self.assertNotEqual(self.path.read_bytes(), previous)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    async def test_same_day_interrupted_append_gets_separate_line_after_restart(self):
        await self.audit.record("tick")
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        restored = DailyProactiveLogger(self.path, NullLogger(), clock=self.clock.now)
        await restored.record("tick")
        self.assertEqual(len(read_rows(self.path)), 3)

    async def test_concurrent_appends_are_complete_and_secret_keys_are_removed(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.audit._write, "score", {"score": i / 40,
                       "api_key": "do-not-save", "nested": {"authorization": "also-secret"}})
                       for i in range(40)]
            for future in futures:
                future.result()
        self.assertEqual(len(read_rows(self.path)), 41)
        self.assertNotIn("do-not-save", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("also-secret", self.path.read_text(encoding="utf-8"))

    async def test_scoped_cleanup_removes_identified_rows_only(self):
        await self.audit.record("tick", session_count=2)
        await self.audit.record("blocked", session_id="s1", user_id="u")
        await self.audit.record("platform_rejected", session_id="s1")
        await self.audit.record("blocked", session_id="s2", user_id="u")
        await self.audit.record("blocked", session_id="other", user_id="other")
        self.assertEqual(self.audit.delete_user("", {"s1"}), 2)
        self.assertTrue(any(row.get("session_id") == "s2" for row in read_rows(self.path)))
        self.assertEqual(self.audit.delete_user("u", set()), 1)
        self.assertTrue(any(row.get("session_id") == "other" for row in read_rows(self.path)))

    async def test_disabled_loop_still_has_lifecycle_and_independent_day_watcher(self):
        loop, *_ = build_loop(self.clock)
        loop._audit_sink = self.audit
        loop._config = replace(loop._config, agent_loop_enabled=False)
        await self.audit.start(lambda: {"loop_enabled": False})
        task = self.audit._task
        await self.audit.start(lambda: {})
        self.assertIs(self.audit._task, task)
        await loop.start()
        self.assertFalse(loop.running)
        self.clock.advance(86400)
        await asyncio.to_thread(self.audit._rollover)
        self.assertEqual(read_rows(self.path)[0]["date"], "2026-10-20")
        await self.audit.close()
        self.assertTrue(task.done())
        self.assertIn("shutdown", [row.get("stage") for row in read_rows(self.path)])

    async def test_probability_silence_is_logged_every_tick_without_reroll_or_model_call(self):
        loop, store, planner, sender, model = await self.make_loop(0.99)
        for _ in range(3):
            await loop.tick()
        rows = read_rows(self.path)
        blocked = [row for row in rows if row.get("stage") == "blocked"]
        self.assertEqual(len(blocked), 3)
        self.assertTrue(all("概率结果选择安静" in row["reason"] for row in blocked))
        self.assertEqual(model.calls, 1)
        self.assertEqual(planner.calls, 0)
        self.assertFalse(sender.messages)

    async def test_success_logs_stages_and_counts_without_chat_or_generated_body(self):
        loop, _, planner, sender, model = await self.make_loop()
        await loop.tick()
        rows = read_rows(self.path)
        stages = {row.get("stage") for row in rows}
        self.assertTrue({"tick", "evaluation", "daily_events", "candidate", "cooldown", "score",
                         "probability", "memory", "planner", "generation", "send_start", "send_result"} <= stages)
        self.assertEqual(next(row for row in rows if row.get("stage") == "send_result")["status"], "accepted")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private user text", text)
        self.assertNotIn(sender.messages[0][1], text)
        self.assertEqual((model.calls, planner.calls, len(sender.messages)), (1, 1, 1))

    async def test_planner_silence_and_active_window_have_explanations(self):
        loop, _, planner, sender, _ = await self.make_loop()
        loop._config = replace(loop._config, active_conversation_window=7200)
        await loop.tick()
        self.assertIn("仍在活跃聊天保护期", self.path.read_text(encoding="utf-8"))
        loop._config = replace(loop._config, active_conversation_window=0)
        planner.action = "do_nothing"
        await loop.tick()
        self.assertIn("not enough context", self.path.read_text(encoding="utf-8"))
        self.assertFalse(sender.messages)

    async def test_platform_false_and_exception_are_distinct_and_never_log_exception_text(self):
        for result in (False, RuntimeError("secret platform body")):
            class Context:
                async def send_message(self, *args):
                    if isinstance(result, Exception):
                        raise result
                    return result
            sender = AstrBotProactiveSender(Context(), NullLogger(), audit=self.audit)
            outcome = await sender.send("s", "private generated message")
            self.assertFalse(outcome)
        rows = read_rows(self.path)
        self.assertTrue({"platform_rejected", "platform_error"} <= {r.get("stage") for r in rows})
        self.assertNotIn("secret platform body", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("private generated message", self.path.read_text(encoding="utf-8"))

    async def test_cancelled_segmented_send_preserves_accepted_prefix_and_records_it(self):
        waiting = asyncio.Event()
        class Context:
            async def send_message(self, *args):
                return True
        async def wait(_):
            waiting.set()
            await asyncio.Event().wait()
        saved = []
        async def save(text):
            saved.append(text)
        sender = AstrBotProactiveSender(Context(), NullLogger(), audit=self.audit,
                    split_enabled=True, split_max_chars=4, sleep_func=wait)
        task = asyncio.create_task(sender.send("s", "第一句。第二句。", on_delivered=save))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(saved, ["第一句。"])
        accepted = [r for r in read_rows(self.path) if r.get("stage") == "platform_accepted"]
        self.assertEqual([r["delivered_chunks"] for r in accepted], [1])

    async def test_diagnostic_io_failure_does_not_change_send_or_model_counts(self):
        loop, _, planner, sender, model = await self.make_loop()
        with patch.object(self.audit, "_write", side_effect=OSError("disk full")):
            await loop.tick()
        self.assertEqual((model.calls, planner.calls, len(sender.messages)), (1, 1, 1))

    async def test_unexpected_evaluation_error_records_stage_and_safe_exception_type(self):
        loop, *_ = await self.make_loop()
        async def broken(*args, **kwargs):
            raise RuntimeError("private state dump")
        loop._states.advance_time = broken
        with self.assertRaises(RuntimeError):
            await loop.tick()
        row = next(r for r in read_rows(self.path) if r.get("stage") == "evaluation_error")
        self.assertEqual(row["error_type"], "RuntimeError")
        self.assertEqual(row["session_id"], "s")
        self.assertNotIn("private state dump", self.path.read_text(encoding="utf-8"))
