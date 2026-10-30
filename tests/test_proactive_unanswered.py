from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.diagnostics import DailyProactiveLogger
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.loop.scheduler import ContextScheduler
from astrbot_plugin_alice_agent.alice_agent.loop.store import InMemoryAutonomyStore, SQLiteAutonomyStore
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.proactive.cooldown import CooldownConfig, CooldownManager
from astrbot_plugin_alice_agent.alice_agent.proactive.daily_policy import DailyProactivePolicy
from astrbot_plugin_alice_agent.alice_agent.proactive.diagnostics import format_status
from astrbot_plugin_alice_agent.alice_agent.proactive.evaluator import ProactiveEvaluator
from astrbot_plugin_alice_agent.alice_agent.proactive.models import ScheduledContextEvent
from astrbot_plugin_alice_agent.alice_agent.proactive.unanswered import unanswered_status
from astrbot_plugin_alice_agent.tests.test_autonomy import FakeClock, PartialFakeSender, build_loop
from astrbot_plugin_alice_agent.tests.test_daily_proactive import Model


HOUR = 3600.0
NOW = datetime(2026, 10, 30, 12).timestamp()


class DailyEvents:
    def __init__(self):
        self.calls = 0

    async def read_cached(self, session_id, timestamp):
        day = datetime.fromtimestamp(timestamp).date().isoformat()
        return [{"event_id": "event_" + day, "day": day, "text": "Alice整理了今天的安排。",
                 "topic_hook": "今天的安排", "importance": 0.35}]

    async def ensure(self, session_id, user_id, timestamp, *args, **kwargs):
        self.calls += 1
        return await self.read_cached(session_id, timestamp)


class Audit:
    def __init__(self):
        self.rows = []

    async def record(self, stage, **details):
        self.rows.append({"stage": stage, **copy.deepcopy(details)})


class UnansweredPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def make_loop(self, store=None):
        loop, original, planner, sender = build_loop(FakeClock(NOW), daily_budget=10)
        store = store or original
        loop._store = loop._drives._store = loop._cooldown._store = store
        loop._scheduler = ContextScheduler(store)
        loop._daily_events = DailyEvents()
        loop._config = replace(loop._config, daily_events_enabled=True, proactive_daily_score_threshold=0)
        model = Model()
        loop._daily_policy = DailyProactivePolicy(store, llm=model, random_value=lambda: 0)
        loop._audit_sink = Audit()
        await loop.on_user_event(AgentEvent("old", "t", "USER_MESSAGE", "test", "s", "u", NOW - 100 * HOUR, "hello"))
        return loop, store, planner, sender, model

    async def test_no_previous_send_does_not_add_a_contact_block(self):
        loop, store, _, sender, model = await self.make_loop()
        await loop.tick()
        self.assertEqual((len(sender.messages), model.calls), (1, 1))
        status = await unanswered_status(store, loop._config, "s", NOW - 100 * HOUR, NOW)
        self.assertEqual(status["recent_unanswered_count"], 1)
        self.assertEqual(status["reason_codes"], ["unanswered_wait"])

    async def test_wait_blocks_before_event_models_draw_and_attempt_claim(self):
        loop, store, planner, sender, model = await self.make_loop()
        await store.record_send("u", "s", "old", NOW - 23 * HOUR)
        await loop.tick()
        self.assertEqual((loop._daily_events.calls, model.calls, planner.calls, len(sender.messages)), (0, 0, 0, 0))
        day = datetime.fromtimestamp(NOW).date().isoformat()
        self.assertIsNone(await store.get_daily_proactive_decision("s", day))
        self.assertEqual((await store.get_daily_proactive_state("s", day))["attempt_count"], 0)
        row = next(row for row in loop._audit_sink.rows if row["stage"] == "unanswered")
        self.assertEqual((row["reason_code"], row["next_evaluable_at"]), ("unanswered_wait", NOW + HOUR))

    async def test_unanswered_message_allows_another_share_at_wait_boundary(self):
        loop, store, _, sender, model = await self.make_loop()
        await store.record_send("u", "s", "old", NOW - 24 * HOUR + 1)
        await loop.tick()
        self.assertFalse(sender.messages)
        loop._clock.advance(1)
        await loop.tick()
        self.assertEqual((len(sender.messages), model.calls), (1, 1))
        status = await unanswered_status(store, loop._config, "s", NOW - 100 * HOUR, loop._clock.now())
        self.assertIn("unanswered_limit", status["reason_codes"])

    async def test_two_unanswered_sends_block_then_expire_from_rolling_window(self):
        loop, store, _, sender, model = await self.make_loop()
        await store.record_send("u", "s", "one", NOW - 50 * HOUR)
        await store.record_send("u", "s", "two", NOW - 25 * HOUR)
        await loop.tick()
        self.assertFalse(sender.messages)
        self.assertEqual(model.calls, 0)
        contact = await unanswered_status(store, loop._config, "s", NOW - 100 * HOUR, NOW)
        self.assertEqual((contact["reason_code"], contact["next_evaluable_at"]), ("unanswered_limit", NOW + 22 * HOUR))
        loop._clock.advance(22 * HOUR)
        await loop.tick()
        self.assertEqual((len(sender.messages), model.calls), (1, 1))

    async def test_new_normal_input_resets_unanswered_streak(self):
        loop, store, _, sender, _ = await self.make_loop()
        await store.record_send("u", "s", "one", NOW - 2 * HOUR)
        await store.record_send("u", "s", "two", NOW - HOUR)
        await loop.tick()
        self.assertFalse(sender.messages)
        await loop.on_user_event(AgentEvent("new", "t", "USER_MESSAGE", "test", "s", "u", NOW, "你好"))
        contact = await unanswered_status(store, loop._config, "s", NOW, NOW)
        self.assertTrue(contact["allowed"])
        self.assertFalse(contact["has_unanswered"])
        self.assertEqual(contact["recent_unanswered_count"], 0)
        await loop.tick()
        self.assertEqual(len(sender.messages), 1)

    async def test_wait_release_does_not_bypass_cooldown_or_saved_silence(self):
        for blocker in ("cooldown", "probability"):
            with self.subTest(blocker=blocker):
                loop, store, _, sender, model = await self.make_loop()
                await store.record_send("u", "s", "old", NOW - 25 * HOUR)
                if blocker == "cooldown":
                    loop._cooldown = CooldownManager(store, CooldownConfig(48 * HOUR, 0, 10))
                else:
                    day = datetime.fromtimestamp(NOW).date().isoformat()
                    await store.save_daily_proactive_decision("s", day, {"eligible": False, "probability": 0.1,
                        "roll": 0.9, "source": "rule", "reason": "silence"})
                await loop.tick()
                self.assertFalse(sender.messages)
                self.assertEqual(model.calls, 0)

    async def test_window_and_inactive_session_guards_remain(self):
        for blocker in ("window", "inactivity"):
            with self.subTest(blocker=blocker):
                loop, store, _, sender, model = await self.make_loop()
                await store.record_send("u", "s", "old", NOW - 25 * HOUR)
                if blocker == "window":
                    loop._evaluator = ProactiveEvaluator("21:00", "23:00")
                else:
                    await store.upsert_session("s", "u", NOW - 8 * 24 * HOUR)
                await loop.tick()
                self.assertEqual((loop._daily_events.calls, model.calls, len(sender.messages)), (0, 0, 0))

    async def test_scheduled_followup_retains_original_contact_exception(self):
        loop, store, _, sender, model = await self.make_loop()
        await store.record_send("u", "s", "one", NOW - 2 * HOUR)
        await store.record_send("u", "s", "two", NOW - HOUR)
        await loop.schedule(ScheduledContextEvent(id="due", session_id="s", user_id="u", title="考试",
            event_type="exam", expected_at=NOW - 1, summary="考试后关心",
            followup_start=NOW - 1, followup_end=NOW + HOUR))
        await loop.tick()
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(model.calls, 0)

    async def test_pre_send_rechecks_contact_if_another_send_was_recorded(self):
        loop, store, _, sender, _ = await self.make_loop()
        original = loop._responses.generate
        async def another_send(*args, **kwargs):
            await store.record_send("u", "s", "parallel", NOW)
            return await original(*args, **kwargs)
        loop._responses.generate = another_send
        await loop.tick()
        self.assertFalse(sender.messages)
        blocked = [row for row in loop._audit_sink.rows if row["stage"] == "pre_send_blocked"]
        self.assertEqual(blocked[-1]["reason_code"], "unanswered_wait")

    async def test_partial_delivery_counts_one_contact_and_blocks_immediate_repeat(self):
        loop, store, _, _, _ = await self.make_loop()
        sender = PartialFakeSender()
        loop._sender = sender
        await loop.tick()
        stats = await store.unanswered_stats("s", NOW - 100 * HOUR, NOW - 72 * HOUR, 2)
        self.assertEqual(stats["recent_count"], 1)
        await loop.tick()
        self.assertEqual(len(sender.messages), 1)

    async def test_status_is_readonly_and_matches_runtime_block_and_deadline(self):
        loop, store, planner, sender, model = await self.make_loop()
        await store.record_send("u", "s", "one", NOW - 50 * HOUR)
        await store.record_send("u", "s", "two", NOW - 25 * HOUR)
        before = copy.deepcopy((store.sessions, store.sends, store.daily_proactive, store.daily_decisions))
        status = await loop.inspect_session("s", "u")
        contact = status["unanswered_policy"]
        self.assertIn("unanswered_limit", status["blockers"])
        self.assertEqual(contact["next_evaluable_at"], NOW + 22 * HOUR)
        self.assertIn("72小时", format_status(status))
        self.assertIn("2/2条", format_status(status))
        self.assertEqual(before, (store.sessions, store.sends, store.daily_proactive, store.daily_decisions))
        self.assertEqual((model.calls, planner.calls, len(sender.messages)), (0, 0, 0))
        await loop.tick()
        actual = next(row for row in loop._audit_sink.rows if row["stage"] == "unanswered")
        self.assertEqual({key: actual[key] for key in contact}, contact)

    async def test_memory_and_sqlite_keep_configured_history_across_maintenance_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autonomy.db"
            config = replace(AgentConfig(), proactive_unanswered_window_seconds=96 * HOUR)
            for store in (InMemoryAutonomyStore(send_retention_seconds=96 * HOUR),
                          SQLiteAutonomyStore(path, send_retention_seconds=96 * HOUR)):
                with self.subTest(store=type(store).__name__):
                    await store.record_send("u", "s", "old", NOW - 70 * HOUR)
                    await store.record_send("u", "s", "new", NOW - 25 * HOUR)
                    await store.record_send("u", "s", "expired", NOW - 100 * HOUR)
                    await store.maintenance(NOW)
                    if isinstance(store, SQLiteAutonomyStore):
                        await store.close()
                        store = SQLiteAutonomyStore(path, send_retention_seconds=96 * HOUR)
                    contact = await unanswered_status(store, config, "s", NOW - 120 * HOUR, NOW)
                    self.assertEqual((contact["recent_unanswered_count"], contact["next_evaluable_at"]), (2, NOW + 26 * HOUR))
                    self.assertTrue((await unanswered_status(store, config, "s", NOW - 120 * HOUR, NOW + 26 * HOUR))["allowed"])
                    await store.close()

    async def test_lowered_limit_uses_nth_newest_expiry_and_is_session_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            for store in (InMemoryAutonomyStore(), SQLiteAutonomyStore(Path(folder) / "scopes.db")):
                with self.subTest(store=type(store).__name__):
                    for hours in (70, 60, 50, 40):
                        await store.record_send("u", "s", "topic", NOW - hours * HOUR)
                    await store.record_send("u", "other-session", "topic", NOW)
                    await store.record_send("other-user", "other-user-session", "topic", NOW)
                    config = AgentConfig()
                    contact = await unanswered_status(store, config, "s", NOW - 100 * HOUR, NOW)
                    self.assertEqual((contact["recent_unanswered_count"], contact["next_evaluable_at"]), (4, NOW + 22 * HOUR))
                    contact = await unanswered_status(store, replace(config, proactive_unanswered_limit=1), "s", NOW - 100 * HOUR, NOW)
                    self.assertEqual(contact["next_evaluable_at"], NOW + 32 * HOUR)
                    self.assertTrue((await unanswered_status(store, config, "new-session", NOW - HOUR, NOW))["allowed"])
                    self.assertTrue((await unanswered_status(store, config, "s", NOW, NOW))["allowed"])
                    await store.close()

    async def test_legacy_retained_timestamp_is_finite_and_not_reset_by_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            old_store = SQLiteAutonomyStore(path, send_retention_seconds=48 * HOUR)
            await old_store.record_send("u", "s", "old", NOW - 60 * HOUR)
            await old_store.maintenance(NOW)
            upgraded = SQLiteAutonomyStore(path)
            status = await unanswered_status(upgraded, AgentConfig(), "s", NOW - 100 * HOUR, NOW)
            self.assertEqual((status["has_unanswered"], status["recent_unanswered_count"], status["allowed"]), (True, 1, True))
            self.assertEqual(status["last_proactive_at"], NOW - 60 * HOUR)
            self.assertEqual(status["history_source"], "last_timestamp_only")
            await upgraded.close()
            await old_store.close()

    async def test_equal_timestamp_is_conservatively_unanswered(self):
        store = InMemoryAutonomyStore()
        await store.record_send("u", "s", "same-time", NOW)
        status = await unanswered_status(store, AgentConfig(), "s", NOW, NOW)
        self.assertFalse(status["allowed"])
        self.assertEqual(status["recent_unanswered_count"], 1)

    async def test_log_explains_contact_count_and_local_deadline_without_content(self):
        with tempfile.TemporaryDirectory() as folder:
            loop, store, _, _, _ = await self.make_loop()
            await store.record_send("u", "s", "private topic", NOW - HOUR)
            audit = DailyProactiveLogger(Path(folder) / "today.log", NullLogger(), clock=loop._clock.now)
            loop._audit_sink = audit
            await loop.tick()
            raw = audit.path.read_text(encoding="utf-8")
            row = next(json.loads(line) for line in raw.splitlines() if json.loads(line).get("stage") == "unanswered")
            self.assertEqual((row["reason_code"], row["recent_unanswered_count"], row["unanswered_limit"]), ("unanswered_wait", 1, 2))
            self.assertEqual(row["next_evaluable_local"], datetime.fromtimestamp(NOW + 23 * HOUR).isoformat())
            self.assertNotIn("private topic", raw)

    async def test_contact_configuration_rejects_nonfinite_boolean_and_invalid_values(self):
        for raw in (float("nan"), float("inf"), True, "bad", None):
            with self.subTest(raw=raw):
                config = AgentConfig(proactive_unanswered_wait_seconds=raw,
                    proactive_unanswered_window_seconds=raw, proactive_unanswered_limit=raw)
                self.assertEqual((config.proactive_unanswered_wait_seconds,
                    config.proactive_unanswered_window_seconds, config.proactive_unanswered_limit), (86400, 259200, 2))
        config = AgentConfig(proactive_unanswered_wait_seconds=-1, proactive_unanswered_window_seconds=0, proactive_unanswered_limit=0)
        self.assertEqual((config.proactive_unanswered_wait_seconds, config.proactive_unanswered_window_seconds, config.proactive_unanswered_limit), (0, 3600, 1))
        config = AgentConfig(proactive_unanswered_wait_seconds=1e10, proactive_unanswered_window_seconds=1e10, proactive_unanswered_limit=999)
        self.assertEqual((config.proactive_unanswered_wait_seconds, config.proactive_unanswered_window_seconds, config.proactive_unanswered_limit), (2592000, 2592000, 10))
