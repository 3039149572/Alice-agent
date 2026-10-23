from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.memory.local_sqlite import LocalSQLiteMemoryAdapter, LocalSQLiteMemoryConfig
from astrbot_plugin_alice_agent.adapters.storage.sqlite import SQLiteStorageAdapter
from astrbot_plugin_alice_agent.adapters.diagnostics.turn_logger import AtomicJSONTurnLogger
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.history import ConversationTurn
from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryMetadata, MemoryItem, MemoryObservation, MemoryQuery
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.memory.data_service import MemoryDataService
from astrbot_plugin_alice_agent.alice_agent.memory.governance import MemoryScope
from astrbot_plugin_alice_agent.alice_agent.memory.metadata_store import SQLiteMemoryMetadataStore


class MemoryGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(self.path / "memory.db", min_score=0))
        self.addAsyncCleanup(self.adapter.close)
        self.metadata = SQLiteMemoryMetadataStore(self.path / "metadata.db")
        self.addAsyncCleanup(self.metadata.close)
        self.now = time.time()
        self.scope = MemoryScope("u", "s")
        self.history = SessionManager(storage=SQLiteStorageAdapter(self.path / "history.db"))
        self.addAsyncCleanup(self.history.close)
        async def invalidate(job):
            for session in job["session_ids"]:
                await self.history.forget(session)
        self.invalidate = invalidate
        self.service = MemoryDataService(self.adapter, self.metadata, invalidate_context=invalidate, clock=lambda: self.now)

    async def observe(self, text, trace, *, user="u", session="s", timestamp=None, fact=True):
        item = MemoryItem(trace, text, importance=0.9, created_at=timestamp or self.now,
                          metadata={"user_id": user, "session_id": session, "trace_id": trace,
                                    "extract_explicit_facts": fact, "topic": "general"})
        observation = MemoryObservation(item, "received", True)
        await self.adapter.observe(observation)
        for saved in observation.persisted_items:
            await self.metadata.save(MemoryMetadata(saved.id, importance=0.9))
        return observation.persisted_items[0].id

    async def token(self, command, scope=None):
        result = await self.service.command(scope or self.scope, command)
        return result.rsplit(" ", 1)[-1]

    async def test_management_cannot_access_other_users_or_out_of_scope_sessions(self):
        own = await self.observe("我住在北京。", "own")
        foreign = await self.observe("我住在上海。", "foreign", user="v", session="other")
        listed = await self.service.command(self.scope, "list")
        self.assertIn(own, listed)
        self.assertNotIn(foreign, listed)
        journal = await self.service.command(self.scope, "journal")
        self.assertIn("北京", journal)
        self.assertNotIn("上海", journal)
        for command in (f"show {foreign}", f"correct {foreign} 我住在天津。", f"delete {foreign}"):
            self.assertIn("未找到", await self.service.command(self.scope, command))
        adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(self.path / "sessions.db", scope="session"))
        self.addAsyncCleanup(adapter.close)
        await adapter.observe(MemoryObservation(MemoryItem("x", "own other session", metadata={"user_id": "u", "session_id": "other"}), None, True))
        self.assertEqual((await adapter.manage("list", self.scope))["total"], 0)
        with self.assertRaises(KeyError):
            await adapter.manage("show", self.scope, "x")

    async def test_fact_correction_changes_current_recall_preserves_provenance_and_clears_context(self):
        original = await self.observe("我住在北京。", "first", timestamp=self.now - 1)
        await self.history.append_and_persist("s", ConversationTurn("old", "北京", self.now))
        result = await self.service.command(self.scope, f"correct {original} 我住在上海。", trace_id="correction")
        self.assertIn("已更正", result)
        records = await self.adapter.recall(MemoryQuery("s", "u", "我现在住哪里", "general", PerceptionResult(), AgentState()))
        self.assertEqual([item.metadata["fact_value"] for item in records], ["上海"])
        self.assertEqual(records[0].metadata["source_type"], "user_correction")
        detail = await self.adapter.manage("show", self.scope, records[0].id)
        self.assertEqual([x["value"] for x in detail["versions"]], ["上海", "北京"])
        self.assertEqual(detail["sources"][0]["evidence"], "我住在上海")
        self.assertEqual(await self.history.load_history("s"), [])
        self.assertEqual(await self.adapter.manage("jobs"), [])
        # A mismatched fact type or inactive version cannot silently create a
        # second unrelated profile fact through the correction command.
        self.assertIn("参数无效", await self.service.command(self.scope, f"correct {original} 我叫小明。"))

    async def test_raw_correction_updates_index_and_retains_edit_history(self):
        memory_id = await self.observe("周日去海边看海鸥", "raw", fact=False)
        await self.service.command(self.scope, f"correct {memory_id} 周日去公园看松鼠", trace_id="edit")
        query = lambda text: MemoryQuery("s", "u", text, "general", PerceptionResult(), AgentState())
        self.assertEqual(await self.adapter.recall(query("海鸥")), [])
        self.assertEqual([x.id for x in await self.adapter.recall(query("松鼠"))], [memory_id])
        detail = await self.adapter.manage("show", self.scope, memory_id)
        self.assertEqual(detail["edits"][0]["old_content"], "周日去海边看海鸥")

    async def test_delete_requires_self_bound_fresh_single_use_confirmation(self):
        memory_id = await self.observe("我住在北京。", "one")
        token = await self.token(f"delete {memory_id}")
        other = await self.service.command(MemoryScope("v", "s"), f"confirm {token}")
        self.assertIn("无效", other)
        self.assertIsNotNone(await self.adapter.get(memory_id))
        # A repeated statement reuses its ID but changes provenance, so the
        # previous preview must no longer authorize deletion.
        await self.observe("我住在北京。", "repeat")
        self.assertIn("数据已变化", await self.service.command(self.scope, f"confirm {token}"))
        token = await self.token(f"delete {memory_id}")
        self.assertIn("已删除", await self.service.command(self.scope, f"confirm {token}"))
        self.assertIn("无效", await self.service.command(self.scope, f"confirm {token}"))

    async def test_delete_removes_entire_fact_lineage_indexes_sources_and_original_turns(self):
        old = await self.observe("我住在北京。", "old", timestamp=self.now - 2)
        await self.observe("我住在上海。", "new", timestamp=self.now - 1)
        foreign = await self.observe("我住在杭州。", "foreign", user="v", session="v")
        token = await self.token(f"delete {old}")
        await self.service.command(self.scope, f"confirm {token}")
        self.assertEqual(await self.adapter.counts(), (1, 1))
        for table in ("memory_search_terms", "memory_search_documents", "memory_fact_versions", "memory_fact_sources"):
            rows = self.adapter._connection.execute(f"SELECT DISTINCT memory_id FROM {table}").fetchall()
            self.assertEqual([row[0] for row in rows], [foreign])
        self.assertEqual(set(await self.metadata.get_many([old, foreign])), {foreign})

    async def test_cleanup_failure_is_durable_and_recovered_by_a_new_instance(self):
        memory_id = await self.observe("private old text", "raw", fact=False)
        await self.history.append_and_persist("s", ConversationTurn("private old text", "reply", self.now))
        actual = self.metadata.delete_many
        async def unavailable(_ids):
            raise OSError("simulated metadata outage")
        self.metadata.delete_many = unavailable
        token = await self.token(f"delete {memory_id}")
        result = await self.service.command(self.scope, f"confirm {token}")
        self.assertIn("未全部完成", result)
        self.assertIsNone(await self.adapter.get(memory_id))
        jobs = await self.adapter.manage("jobs")
        self.assertEqual(len(jobs), 1)
        self.assertNotIn("private old text", str(jobs))
        self.metadata.delete_many = actual
        await self.adapter.close()
        reopened = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(self.path / "memory.db"))
        self.addAsyncCleanup(reopened.close)
        service = MemoryDataService(reopened, self.metadata, invalidate_context=self.invalidate)
        await service.recover()
        self.assertEqual(await reopened.manage("jobs"), [])
        self.assertEqual(await self.history.load_history("s"), [])
        self.assertEqual(await self.metadata.get_many([memory_id]), {})

    async def test_retention_zero_preserves_and_opt_in_respects_pins_and_current_versions(self):
        old_time = self.now - 40 * 86400
        old = await self.observe("我住在北京。", "old", timestamp=old_time)
        current = await self.observe("我住在上海。", "new", timestamp=self.now - 1)
        pinned = await self.observe("kept pin", "pin", timestamp=old_time, fact=False)
        await self.metadata.save(MemoryMetadata(pinned, pinned=True))
        self.adapter._connection.execute("UPDATE memories SET updated_at=created_at")
        self.adapter._connection.commit()
        await self.service.maintain(force=True)
        self.assertEqual(await self.adapter.counts(), (3, 3))
        service = MemoryDataService(self.adapter, self.metadata, inactive_fact_days=10,
                                    journal_days=5, memory_days=30, clock=lambda: self.now)
        await service.maintain(force=True)
        self.assertIsNone(await self.adapter.get(old))
        self.assertIsNotNone(await self.adapter.get(current))
        self.assertIsNotNone(await self.adapter.get(pinned))
        self.assertEqual(await self.adapter.counts(), (2, 1))
        self.assertEqual(await self.metadata.get_many([old]), {})

    async def test_pin_read_failure_prevents_automatic_deletion(self):
        await self.observe("kept", "raw", timestamp=self.now - 40 * 86400, fact=False)
        self.adapter._connection.execute("UPDATE memories SET updated_at=created_at")
        self.adapter._connection.commit()
        async def unavailable():
            raise OSError("pin state unavailable")
        self.metadata.pinned_ids = unavailable
        service = MemoryDataService(self.adapter, self.metadata, memory_days=1, clock=lambda: self.now)
        with self.assertRaises(OSError):
            await service.maintain(force=True)
        self.assertEqual(await self.adapter.counts(), (1, 1))

    async def test_inactive_fact_retention_does_not_expire_current_fact(self):
        old = await self.observe("我住在北京。", "old", timestamp=self.now - 40 * 86400)
        current = await self.observe("我住在上海。", "new", timestamp=self.now - 20 * 86400)
        service = MemoryDataService(self.adapter, self.metadata, inactive_fact_days=10, clock=lambda: self.now)
        await service.maintain(force=True)
        self.assertIsNone(await self.adapter.get(old))
        self.assertIsNotNone(await self.adapter.get(current))

    async def test_confirmation_expires_and_atomic_delete_rolls_back_if_job_cannot_be_saved(self):
        memory_id = await self.observe("kept", "raw", fact=False)
        clock = [10.0]
        self.service._token_clock = lambda: clock[0]
        token = await self.token(f"delete {memory_id}")
        clock[0] += 300
        self.assertIn("过期", await self.service.command(self.scope, f"confirm {token}"))
        self.assertIsNotNone(await self.adapter.get(memory_id))
        preview = await self.adapter.manage("preview", self.scope, memory_id)
        original = self.adapter._data._job
        def fail(*args, **kwargs):
            raise OSError("job disk full")
        self.adapter._data._job = fail
        with self.assertRaises(OSError):
            await self.adapter.manage("delete", self.scope, memory_id, preview["fingerprint"])
        self.adapter._data._job = original
        self.assertEqual(await self.adapter.counts(), (1, 1))
        self.assertIsNotNone(await self.adapter.get(memory_id))

    async def test_cancellation_waits_for_deletion_and_context_invalidation(self):
        memory_id = await self.observe("private", "raw", fact=False)
        token = await self.token(f"delete {memory_id}")
        entered, release = asyncio.Event(), asyncio.Event()
        actual = self.metadata.delete_many
        async def slow(ids):
            entered.set()
            await release.wait()
            await actual(ids)
        self.metadata.delete_many = slow
        task = asyncio.create_task(self.service.command(self.scope, f"confirm {token}"))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await self.adapter.get(memory_id))
        self.assertEqual(await self.adapter.manage("jobs"), [])
        self.assertEqual(await self.metadata.get_many([memory_id]), {})

    async def test_private_command_bypasses_models_and_does_not_register_a_session(self):
        from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin
        from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
        from astrbot_plugin_alice_agent.adapters.astrbot.message_filter import AlicePrivateMessageFilter
        plugin = _make_plugin(self, lambda _event: self.fail("data commands must not generate"))
        event = FakeEvent("/alice-data list")
        self.assertFalse(AlicePrivateMessageFilter().filter(event, {"wake_prefix": []}))
        replies = [item async for item in plugin.alice_data(event)]
        self.assertIn("当前记忆共0条", replies[0][1])
        self.assertTrue(event.stopped)
        self.assertEqual(await plugin._agent_loop._store.list_sessions(), [])
        plugin._agent.handle_event.assert_not_called()

    async def test_user_gate_and_epoch_prevent_stale_work_without_blocking_other_users(self):
        event = AgentEvent("id", "trace", "private", "test", "s", "u", self.now, "old")
        self.service.stamp(event)
        entered, release, deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def in_flight():
            async with self.service.guard("u"):
                async with self.service.guard("u"):
                    entered.set()
                    await release.wait()
        async def deletion():
            async with self.service.guard("u"):
                self.service.invalidate_user("u")
                deleted.set()
        task = asyncio.create_task(in_flight())
        await entered.wait()
        deleting = asyncio.create_task(deletion())
        async with asyncio.timeout(1):
            async with self.service.guard("other"):
                self.assertFalse(deleted.is_set())
        release.set()
        await asyncio.gather(task, deleting)
        self.assertFalse(self.service.is_current(event))

    async def test_log_retention_runs_without_new_diagnostic_writes_and_remains_self_scoped(self):
        logs = AtomicJSONTurnLogger(self.path / "logs")
        path = logs.write({"schema_version": "alice_turn_test_v1", "trace_id": "t", "user_id": "u", "session_id": "s"})
        foreign = logs.write({"schema_version": "alice_turn_test_v1", "trace_id": "v", "user_id": "v", "session_id": "v"})
        unrelated = self.path / "logs" / "manual.json"
        unrelated.write_text('{"user_id":"u"}', encoding="utf-8")
        self.assertEqual(logs.delete_user("u"), 1)
        self.assertTrue(foreign.exists())
        self.assertTrue(unrelated.exists())
        os.utime(foreign, (self.now - 20 * 86400, self.now - 20 * 86400))
        async def extra(now):
            await asyncio.to_thread(logs.cleanup_expired, 5, now=now)
        service = MemoryDataService(self.adapter, self.metadata, maintenance_extra=extra, clock=lambda: self.now)
        await service.start()
        await service.close()
        self.assertFalse(foreign.exists())
        self.assertTrue(unrelated.exists())

    async def test_idle_session_snapshots_are_physically_pruned_without_returning_users(self):
        storage = SQLiteStorageAdapter(self.path / "idle.db")
        clock = [self.now]
        sessions = SessionManager(storage=storage, clock=lambda: clock[0], max_age_seconds=3600)
        self.addAsyncCleanup(sessions.close)
        await sessions.append_and_persist("idle", ConversationTurn("old private", "reply", self.now))
        key = sessions._storage_key("idle")
        self.assertIsNotNone(await storage.get("alice_session_history", key))
        clock[0] += 7200
        await sessions.maintenance()
        self.assertIsNone(await storage.get("alice_session_history", key))
        self.assertEqual(sessions.history("idle"), [])

    async def test_adapter_delete_clears_derived_text_but_preserves_numeric_state_and_cooldown(self):
        from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin
        from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
        plugin = _make_plugin(self, lambda _event: self.fail("no model during delete"))
        event = plugin._message_adapter.to_agent_event(FakeEvent("/alice-data"))
        scope = MemoryScope(event.user_id, event.session_id)
        states = plugin._agent._pipeline._state_manager
        await states.commit_update(scope.session_id, AgentState(mood_valence=0.37, current_topic="private topic",
                                   daily_event_day="2026-10-22", daily_events=["private daily"], topic_hooks=["private hook"]))
        store = plugin._agent_loop._store
        await store.upsert_session(scope.session_id, scope.user_id, self.now)
        await store.record_send(scope.user_id, scope.session_id, "topic", self.now)
        before = await store.send_stats(scope.user_id, "topic", self.now - 86400, scope.session_id)
        preview = await plugin._data_service.command(scope, "clear-memory")
        token = preview.rsplit(" ", 1)[-1]
        self.assertIn("已删除", await plugin._data_service.command(scope, f"confirm {token}"))
        after = await states.get(scope.session_id)
        self.assertEqual(after.mood_valence, 0.37)
        self.assertEqual(after.daily_event_day, "2026-10-22")
        self.assertEqual(after.daily_events, [])
        self.assertEqual(after.topic_hooks, [])
        self.assertIsNone(after.current_topic)
        self.assertEqual(await store.send_stats(scope.user_id, "topic", self.now - 86400, scope.session_id), before)

    async def test_old_waiting_batch_finishes_after_clear_instead_of_spinning(self):
        from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin
        from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
        plugin = _make_plugin(self, lambda _event: self.fail("erased input must not generate"), response_timing_enabled=True)
        prepared = await plugin._prepare_pending_message(FakeEvent("旧内容"))
        batch, item, _owner = prepared
        scope = MemoryScope(item.agent_event.user_id, item.agent_event.session_id)
        preview = await plugin._data_service.command(scope, "clear-memory")
        await plugin._data_service.command(scope, "confirm " + preview.rsplit(" ", 1)[-1])
        async def collect():
            return [value async for value in plugin._run_pending_batch(batch)]
        self.assertEqual(await asyncio.wait_for(collect(), 1), [])
        self.assertIn(batch.phase, {"cancelled", "done"})

    async def test_long_journal_command_is_bounded_output_without_new_chat_writes(self):
        from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin
        from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
        plugin = _make_plugin(self, lambda _event: self.fail("journal must not generate"))
        event = FakeEvent("/alice-data journal")
        mapped = plugin._message_adapter.to_agent_event(event)
        port = plugin._data_service._port
        for index in range(10):
            item = MemoryItem(str(index), "用户原文" * 130, created_at=self.now + index,
                              metadata={"user_id": mapped.user_id, "session_id": mapped.session_id, "trace_id": str(index)})
            # Volatile memory normally opts out of raw journaling; turn it on
            # only in this fixture so the command has actual stored rows.
            from dataclasses import replace
            port._config = replace(port._config, capture_all_turns=True)
            await port.observe(MemoryObservation(item, "实际回复" * 130, False))
        before = await port.counts()
        outputs = [value[1] async for value in plugin.alice_data(event)]
        self.assertGreater(len(outputs), 1)
        self.assertTrue(all(len(value) <= 1000 for value in outputs))
        self.assertIn("原始轮次共10条", "".join(outputs))
        self.assertEqual("".join(outputs).count("用户："), 10)
        self.assertEqual(before, await port.counts())
