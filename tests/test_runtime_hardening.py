from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_alice_agent.adapters.diagnostics import AtomicJSONTurnLogger
from astrbot_plugin_alice_agent.adapters.storage.in_memory import InMemoryStorageAdapter
from astrbot_plugin_alice_agent.adapters.storage.sqlite import SQLiteStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.decision.metrics import DecisionMetrics
from astrbot_plugin_alice_agent.alice_agent.decision.models import DecisionMode
from astrbot_plugin_alice_agent.alice_agent.decision.schemas import planner_questions
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.drives.models import DriveState
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer
from astrbot_plugin_alice_agent.alice_agent.state.time_reducer import TimeStateReducer
from astrbot_plugin_alice_agent.main import AliceAgentPlugin, _build_media_config
from astrbot_plugin_alice_agent import main as plugin_main
from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeContext
from astrbot_plugin_alice_agent.tests.test_autonomy import FakeClock, build_loop
from astrbot_plugin_alice_agent.tests.test_decision_layer import MockDecisionModel


class RestLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_ticks_use_total_inactivity_and_rest_recovers(self) -> None:
        clock = FakeClock(1000.0)
        loop, store, _, _ = build_loop(clock)
        self.addAsyncCleanup(loop.close)
        await loop.on_user_event(AgentEvent("e", "t", "private", "test", "s", "u", clock.now(), "hello"))
        await loop._states.commit_update("s", AgentState(activity="chatting"))
        await store.save_drive(DriveState("s", "u", rest_need=0.8, updated_at=clock.now()))
        for _ in range(29):
            clock.advance(30)
            await loop.tick()
        self.assertEqual((await loop._states.get("s")).activity, "chatting")
        clock.advance(30)
        await loop.tick()
        self.assertEqual((await loop._states.get("s")).activity, "idle")
        for _ in range(30):
            clock.advance(30)
            await loop.tick()
        self.assertEqual((await loop._states.get("s")).activity, "resting")
        before = (await store.get_drive("s")).rest_need
        for _ in range(120):
            clock.advance(30)
            await loop.tick()
        self.assertLess((await store.get_drive("s")).rest_need, before)

    async def test_rest_exits_after_recovery_and_new_message_wakes_state(self) -> None:
        reducer = TimeStateReducer()
        recovered = reducer.reduce(
            AgentState(activity="resting", energy=0.65), 30,
            inactivity_seconds=86400, rest_need=0.19,
        )
        self.assertEqual(recovered.activity, "idle")
        resting = AgentState(activity="resting", energy=0.3)
        message = AgentEvent("e", "t", "private", "test", "s", "u", 1000, "在吗")
        awake = StateReducer().reduce(resting, PerceptionResult(), message)
        self.assertEqual(awake.activity, "chatting")
        self.assertEqual(reducer.reduce(
            awake, 30, inactivity_seconds=30, rest_need=0.9,
        ).activity, "chatting")

    async def test_low_energy_can_rest_and_valence_returns_without_daily_events(self) -> None:
        reducer = TimeStateReducer()
        state = AgentState(activity="idle", energy=0.3, mood_valence=-0.8)
        updated = reducer.reduce(state, 30, inactivity_seconds=1800)
        self.assertEqual(updated.activity, "resting")
        for _ in range(48):
            updated = reducer.reduce(updated, 3600, inactivity_seconds=86400)
        self.assertAlmostEqual(updated.mood_valence, 0.0)
        self.assertGreater(updated.energy, state.energy)
        self.assertEqual(updated.activity, "idle")

    async def test_disabled_rest_drive_does_not_trap_existing_rest_state(self) -> None:
        clock = FakeClock(1000.0)
        loop, store, _, _ = build_loop(clock)
        self.addAsyncCleanup(loop.close)
        loop._config = replace(loop._config, drive_rest_enabled=False)
        await loop.on_user_event(AgentEvent("e", "t", "private", "test", "s", "u", clock.now(), "hello"))
        await loop._states.commit_update("s", AgentState(activity="resting", energy=0.65))
        await store.save_drive(DriveState("s", "u", rest_need=0.9, updated_at=clock.now()))
        clock.advance(3600)
        await loop.tick()
        self.assertEqual((await loop._states.get("s")).activity, "idle")


class DecisionMetricsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sqlite_reload_preserves_counts_and_adds_new_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metrics.db"
            first = DecisionService(MockDecisionModel(), mode=DecisionMode.SHADOW,
                                    metrics_storage=SQLiteStorageAdapter(path))
            await first.decide("private-input", planner_questions([]), decision_type="planner",
                               schema_version="v1", trace_id="private-trace")
            first.record_comparison("planner", "private-trace", "reply", "wait")
            first.record_escalation("planner")
            first.record_llm_avoided("planner")
            await first.close()
            self.assertTrue(first._metrics_task.done())
            second = DecisionService(MockDecisionModel(), mode=DecisionMode.SHADOW,
                                     metrics_storage=SQLiteStorageAdapter(path))
            try:
                await asyncio.gather(second.start(), second.start())
                self.assertEqual(second.metrics.snapshot(), first.metrics.snapshot())
                await second.decide("different", planner_questions([]), decision_type="planner",
                                    schema_version="v1", trace_id="another-trace")
                await second.flush_metrics()
                raw = await second._metrics_storage.get("decision_metrics", "totals")
                self.assertEqual(raw["buckets"]["planner"]["calls"], 2)
                self.assertEqual(raw["buckets"]["planner"]["existing_disagreements"], 1)
                serialized = json.dumps(raw)
                self.assertNotIn("private", serialized)
                self.assertNotIn("trace", serialized)
                self.assertNotIn("reply", serialized)
            finally:
                await second.close()

    async def test_corrupt_buckets_are_ignored_and_derived_values_recomputed(self) -> None:
        metrics = DecisionMetrics()
        metrics.bucket("planner").calls = 1
        metrics.restore({"schema_version": 1, "buckets": {
            "planner": {"calls": 2, "latency_total_ms": 12.0, "latency_avg_ms": 999},
            "bad_nan": {"latency_total_ms": float("nan")},
            "bad_negative": {"calls": -1}, "bad_bool": {"calls": True},
            "bad_huge": {"latency_total_ms": 10**400},
            "not a task": {"calls": 99},
        }})
        self.assertEqual(list(metrics.snapshot()), ["planner"])
        self.assertEqual(metrics.snapshot()["planner"]["calls"], 3)
        self.assertEqual(metrics.snapshot()["planner"]["latency_avg_ms"], 4.0)
        metrics.restore({"schema_version": 2, "buckets": {"planner": {"calls": 99}}})
        metrics.restore({"schema_version": True, "buckets": {"planner": {"calls": 99}}})
        self.assertEqual(metrics.bucket("planner").calls, 3)

    async def test_storage_read_failure_does_not_overwrite_old_data_or_break_decisions(self) -> None:
        class Unreadable(InMemoryStorageAdapter):
            writes = 0

            async def get(self, *args):
                raise OSError("private storage error")

            async def set(self, *args):
                self.writes += 1

        storage = Unreadable()
        service = DecisionService(MockDecisionModel(), mode=DecisionMode.SHADOW, metrics_storage=storage)
        try:
            result = await service.decide("hello", planner_questions([]), decision_type="planner",
                                          schema_version="v1", trace_id="t")
            self.assertIsNotNone(result)
            await service.flush_metrics()
        finally:
            await service.close()
        self.assertEqual(storage.writes, 0)
        self.assertEqual(service.metrics.bucket("planner").calls, 1)

    async def test_write_failure_retries_next_flush(self) -> None:
        class InitiallyBroken(InMemoryStorageAdapter):
            broken = True

            async def set(self, *args):
                if self.broken:
                    raise OSError("disk full")
                await super().set(*args)

        storage = InitiallyBroken()
        service = DecisionService(None, metrics_storage=storage)
        try:
            await service.start()
            service.metrics.bucket("planner").calls = 3
            await service.flush_metrics()
            storage.broken = False
            await service.flush_metrics()
            self.assertEqual((await storage.get("decision_metrics", "totals"))["buckets"]["planner"]["calls"], 3)
        finally:
            await service.close()

    async def test_periodic_save_and_close_finish_inflight_write_before_final_snapshot(self) -> None:
        class BlockingStorage(InMemoryStorageAdapter):
            started = asyncio.Event()
            release = asyncio.Event()
            writes = 0

            async def set(self, *args):
                self.writes += 1
                if self.writes == 1:
                    self.started.set()
                    await self.release.wait()
                await super().set(*args)

            async def close(self):
                pass

        storage = BlockingStorage()
        service = DecisionService(None, metrics_storage=storage, metrics_flush_seconds=1)
        await service.start()
        service.metrics.bucket("planner").calls = 1
        closing = None
        try:
            await asyncio.wait_for(storage.started.wait(), 3)
            service.metrics.bucket("planner").calls = 2
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
        finally:
            storage.release.set()
            if closing is not None:
                await closing
            else:
                await service.close()
        self.assertEqual((await storage.get("decision_metrics", "totals"))["buckets"]["planner"]["calls"], 2)
        self.assertTrue(service._metrics_task.done())

    async def test_plugin_wires_sqlite_and_disable_switch_leaves_existing_totals_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            plugin_main.StarTools, "get_data_dir", return_value=Path(temporary)
        ):
            config = {"vad_enabled": False, "memory_backend": "in_memory",
                      "session_persistence_enabled": False, "agent_loop_enabled": False}
            first = AliceAgentPlugin(FakeContext(), config)
            await first.initialize()
            first._decision_service.metrics.bucket("planner").calls = 7
            await first.terminate()
            second = AliceAgentPlugin(FakeContext(), config)
            await second.initialize()
            self.assertEqual(second._decision_service.metrics.bucket("planner").calls, 7)
            await second.terminate()
            third = AliceAgentPlugin(FakeContext(), {**config, "decision_metrics_persistence_enabled": False})
            await third.initialize()
            self.assertEqual(third._decision_service.metrics.snapshot(), {})
            third._decision_service.metrics.bucket("planner").calls = 99
            await third.terminate()
            stored = await SQLiteStorageAdapter(Path(temporary) / "alice_metrics.db").get("decision_metrics", "totals")
            self.assertEqual(stored["buckets"]["planner"]["calls"], 7)


class DiagnosticRetentionTests(unittest.TestCase):
    def test_retention_removes_only_expired_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logger = AtomicJSONTurnLogger(root / "turns")
            old = logger.write({"trace_id": "old", "text": "old"})
            recent = logger.write({"trace_id": "new", "text": "new"})
            unrelated = old.parent / "important.json"
            unrelated.write_text("{}")
            folder = old.parent / "20260101T000000_000000Z_folder_12345678.json"
            folder.mkdir()
            now = 2_000_000_000
            for path in (old, unrelated, folder):
                os.utime(path, (now - 10 * 86400, now - 10 * 86400))
            os.utime(recent, (now, now))
            self.assertEqual(logger.cleanup_expired(0, now=now), 0)
            self.assertTrue(old.exists())
            self.assertEqual(logger.cleanup_expired(7, now=now), 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(folder.is_dir())

    def test_disabled_logging_does_not_clean_up_and_opt_in_write_does(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = AtomicJSONTurnLogger(Path(temporary))
            old = logger.write({"trace_id": "old"})
            os.utime(old, (0, 0))
            logger.write({"trace_id": "disabled"}, enabled=lambda: False, retention_days=7)
            self.assertTrue(old.exists())
            logger.write({"trace_id": "enabled"}, retention_days=7)
            self.assertFalse(old.exists())

    def test_media_raw_input_logging_requires_explicit_switch(self) -> None:
        self.assertFalse(_build_media_config({"test_log_enabled": True}).log_tts_inputs)
        self.assertTrue(_build_media_config({"media_log_tts_inputs": True}).log_tts_inputs)


if __name__ == "__main__":
    unittest.main()
