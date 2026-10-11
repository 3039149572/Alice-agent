from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_alice_agent.adapters.storage import InMemoryStorageAdapter, SQLiteStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.history import ConversationTurn
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.main import AliceAgentPlugin, _build_config
from astrbot_plugin_alice_agent import main as plugin_main
from astrbot_plugin_alice_agent.adapters.astrbot.message_adapter import AstrBotMessageAdapter
from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeContext, FakeEvent


_NAMESPACE = "alice_session_history"
_SCHEMA = "alice_session_history_v1"


class RecordingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message, *args): self.messages.append(str(message))
    def info(self, message, *args): self.messages.append(str(message))
    def warning(self, message, *args): self.messages.append(str(message))
    def error(self, message, *args): self.messages.append(str(message))
    def exception(self, message, *args): self.messages.append(str(message))


class FlakyStorage:
    def __init__(self, *, fail_read: bool = False, fail_first_write: bool = False) -> None:
        self.inner = InMemoryStorageAdapter()
        self.fail_read = fail_read
        self.fail_first_write = fail_first_write
        self.read_calls = 0
        self.write_calls = 0

    async def get(self, namespace: str, key: str):
        self.read_calls += 1
        if self.fail_read:
            raise OSError("storage unavailable")
        return await self.inner.get(namespace, key)

    async def set(self, namespace: str, key: str, value) -> None:
        self.write_calls += 1
        if self.fail_first_write and self.write_calls == 1:
            raise OSError("storage unavailable")
        await self.inner.set(namespace, key, value)

    async def delete(self, namespace: str, key: str) -> None:
        await self.inner.delete(namespace, key)

    async def close(self) -> None:
        await self.inner.close()


class DelayedStorage(FlakyStorage):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()
        self.closed = False

    async def set(self, namespace: str, key: str, value) -> None:
        self.write_calls += 1
        self.write_started.set()
        await self.allow_write.wait()
        await self.inner.set(namespace, key, value)

    async def close(self) -> None:
        self.closed = True
        await self.inner.close()


class SessionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_reload_is_session_isolated_bounded_and_text_limited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sessions.db"
            now = time.time()
            first = SessionManager(
                history_limit=3,
                storage=SQLiteStorageAdapter(db_path),
                clock=lambda: now,
            )
            for index in range(5):
                await first.append_and_persist(
                    "session-a",
                    ConversationTurn(f"用户{index}", f"回复{index}", now + min(index, 2)),
                )
            await first.append_and_persist(
                "session-b",
                ConversationTurn("独立用户", "独立回复" * 2500, now),
            )
            await first.close()

            second = SessionManager(
                history_limit=3,
                storage=SQLiteStorageAdapter(db_path),
                clock=lambda: now,
            )
            history_a, history_b = await asyncio.gather(
                second.load_history("session-a"),
                second.load_history("session-b"),
            )
            self.assertEqual([turn.user_text for turn in history_a], ["用户2", "用户3", "用户4"])
            self.assertEqual([turn.assistant_text for turn in history_a], ["回复2", "回复3", "回复4"])
            self.assertEqual([turn.user_text for turn in history_b], ["独立用户"])
            self.assertEqual(len(history_b[0].assistant_text or ""), 8000)
            await second.close()

    async def test_snapshot_is_capped_at_64_turns_and_uses_recent_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sessions.db"
            now = 2_000_000_000.0
            storage = SQLiteStorageAdapter(db_path)
            manager = SessionManager(
                history_limit=100,
                storage=storage,
                clock=lambda: now,
                max_age_seconds=72 * 3600,
            )
            for index in range(70):
                manager.append("cap", ConversationTurn(str(index), None, now - index))
            await manager.append_and_persist(
                "cap", ConversationTurn("tail", None, now + 120)
            )
            await manager.close()

            restored = SessionManager(
                history_limit=100,
                storage=SQLiteStorageAdapter(db_path),
                clock=lambda: now,
            )
            history = await restored.load_history("cap")
            self.assertEqual(len(history), 64)
            self.assertEqual(history[-1].user_text, "tail")
            self.assertEqual(history[0].user_text, "7")
            await restored.close()

    async def test_expired_and_malformed_rows_are_discarded_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            now = 2_000_000_000.0
            storage = SQLiteStorageAdapter(Path(temporary) / "sessions.db")
            logger = RecordingLogger()
            manager = SessionManager(
                storage=storage,
                logger=logger,
                clock=lambda: now,
                max_age_seconds=72 * 3600,
            )
            key = manager._storage_key("session")
            await storage.set(_NAMESPACE, key, {
                "schema_version": _SCHEMA,
                "turns": [
                    {"user_text": "保留", "assistant_text": "近未来", "timestamp": now + 120},
                    {"user_text": "过期", "assistant_text": None, "timestamp": now - 73 * 3600},
                    {"user_text": "布尔时间", "assistant_text": None, "timestamp": True},
                    {"user_text": "布尔时间2", "assistant_text": None, "timestamp": False},
                    {"user_text": "超大整数时间", "assistant_text": None, "timestamp": 10**400},
                    {"user_text": "未来异常", "assistant_text": None, "timestamp": now + 301},
                    {"user_text": 7, "assistant_text": None, "timestamp": now},
                ],
            })
            db = sqlite3.connect(Path(temporary) / "sessions.db")
            try:
                db.execute(
                    "UPDATE alice_values SET payload=replace(payload, '\"timestamp\":true', '\"timestamp\":Infinity') "
                    "WHERE namespace=? AND key=?",
                    (_NAMESPACE, key),
                )
                db.commit()
            finally:
                db.close()
            restored = await manager.load_history("session")
            self.assertEqual([(turn.user_text, turn.assistant_text) for turn in restored], [("保留", "近未来")])
            self.assertTrue(all("近未来" not in message for message in logger.messages))
            await manager.close()

            non_finite = FlakyStorage()
            await non_finite.inner.set(_NAMESPACE, SessionManager._storage_key("inf"), {
                "schema_version": _SCHEMA,
                "turns": [{"user_text": "不应恢复", "assistant_text": None, "timestamp": float("inf")}],
            })
            inf_manager = SessionManager(storage=non_finite, clock=lambda: now)
            self.assertEqual(await inf_manager.load_history("inf"), [])
            await inf_manager.close()

    async def test_invalid_schema_is_ignored_and_read_failure_does_not_overwrite_snapshot(self) -> None:
        logger = RecordingLogger()
        bad_read = FlakyStorage(fail_read=True)
        manager = SessionManager(storage=bad_read, logger=logger)
        await manager.append_and_persist("session", ConversationTurn("新输入", "新回复", time.time()))
        await manager.load_history("session")
        self.assertEqual(bad_read.read_calls, 1)
        self.assertEqual(bad_read.write_calls, 0)
        self.assertEqual(manager.history("session")[0].user_text, "新输入")
        self.assertTrue(any("OSError" in message for message in logger.messages))
        await manager.close()

        with tempfile.TemporaryDirectory() as temporary:
            storage = SQLiteStorageAdapter(Path(temporary) / "sessions.db")
            broken = SessionManager(storage=storage)
            await storage.set(_NAMESPACE, broken._storage_key("broken"), {
                "schema_version": "unknown", "turns": [],
            })
            self.assertEqual(await broken.load_history("broken"), [])
            await broken.append_and_persist(
                "broken", ConversationTurn("安全回退", "可以继续", time.time())
            )
            self.assertEqual(len(await broken.load_history("broken")), 1)
            await broken.close()

    async def test_malformed_sqlite_json_can_be_replaced_by_next_committed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sessions.db"
            seed = SessionManager(storage=SQLiteStorageAdapter(db_path))
            await seed.append_and_persist(
                "session", ConversationTurn("旧快照", "旧回复", time.time())
            )
            key = seed._storage_key("session")
            await seed.close()
            db = sqlite3.connect(db_path)
            try:
                db.execute(
                    "UPDATE alice_values SET payload='not-json' WHERE namespace=? AND key=?",
                    (_NAMESPACE, key),
                )
                db.commit()
            finally:
                db.close()

            logger = RecordingLogger()
            recovering = SessionManager(
                storage=SQLiteStorageAdapter(db_path), logger=logger
            )
            self.assertEqual(await recovering.load_history("session"), [])
            self.assertTrue(any("JSONDecodeError" in message for message in logger.messages))
            await recovering.append_and_persist(
                "session", ConversationTurn("修复后的轮次", "新回复", time.time())
            )
            await recovering.close()

            restored = SessionManager(storage=SQLiteStorageAdapter(db_path))
            history = await restored.load_history("session")
            self.assertEqual([turn.user_text for turn in history], ["修复后的轮次"])
            await restored.close()

    async def test_write_failure_keeps_memory_and_retries_on_next_append(self) -> None:
        storage = FlakyStorage(fail_first_write=True)
        manager = SessionManager(storage=storage)
        now = time.time()
        await manager.append_and_persist("session", ConversationTurn("第一轮", None, now))
        self.assertEqual(len(manager.history("session")), 1)
        await manager.append_and_persist("session", ConversationTurn("第二轮", None, now + 1))
        self.assertEqual(storage.write_calls, 2)
        saved = await storage.inner.get(_NAMESPACE, manager._storage_key("session"))
        self.assertEqual([item["user_text"] for item in saved["turns"]], ["第一轮", "第二轮"])
        await manager.close()

    async def test_concurrent_first_load_and_append_do_not_lose_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sessions.db"
            now = time.time()
            seed = SessionManager(storage=SQLiteStorageAdapter(db_path))
            await seed.append_and_persist("session", ConversationTurn("已存", "上文", now))
            await seed.close()
            current = SessionManager(storage=SQLiteStorageAdapter(db_path))
            await asyncio.gather(
                current.load_history("session"),
                current.append_and_persist(
                    "session", ConversationTurn("新消息", "新回复", now + 1)
                ),
                current.append_and_persist(
                    "session", ConversationTurn("并发消息", "并发回复", now + 2)
                ),
            )
            await current.close()
            restored = SessionManager(storage=SQLiteStorageAdapter(db_path))
            values = [turn.user_text for turn in await restored.load_history("session")]
            self.assertEqual(values, ["已存", "新消息", "并发消息"])
            await restored.close()

    async def test_sync_append_before_first_load_is_merged_after_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sessions.db"
            seed = SessionManager(storage=SQLiteStorageAdapter(db_path))
            await seed.append_and_persist(
                "session", ConversationTurn("磁盘上文", "旧回复", time.time())
            )
            await seed.close()

            current = SessionManager(storage=SQLiteStorageAdapter(db_path))
            current.append("session", ConversationTurn("同步追加", "仍在内存", time.time()))
            history = await current.load_history("session")
            self.assertEqual(
                [turn.user_text for turn in history], ["磁盘上文", "同步追加"]
            )
            await current.close()

    async def test_history_limit_zero_and_disabled_persistence_do_not_read_or_write(self) -> None:
        storage = FlakyStorage()
        manager = SessionManager(history_limit=0, storage=storage)
        await manager.load_history("session")
        await manager.append_and_persist("session", ConversationTurn("忽略", None, time.time()))
        self.assertEqual((storage.read_calls, storage.write_calls), (0, 0))
        await manager.close()

        self.assertTrue(_build_config({}).session_persistence_enabled)
        self.assertEqual(_build_config({"history_restore_max_age_hours": float("nan")}).history_restore_max_age_hours, 72.0)
        self.assertEqual(_build_config({"history_restore_max_age_hours": 999}).history_restore_max_age_hours, 168.0)
        self.assertEqual(_build_config({"history_restore_max_age_hours": 0}).history_restore_max_age_hours, 1.0)

    async def test_cancelled_write_finishes_before_cancellation_and_close(self) -> None:
        storage = DelayedStorage()
        manager = SessionManager(storage=storage)
        append_task = asyncio.create_task(manager.append_and_persist(
            "session", ConversationTurn("输入", "已进入内存", time.time())
        ))
        await storage.write_started.wait()
        append_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(append_task.done())
        storage.allow_write.set()
        with self.assertRaises(asyncio.CancelledError):
            await append_task
        saved = await storage.inner.get(_NAMESPACE, manager._storage_key("session"))
        self.assertEqual(saved["turns"][0]["user_text"], "输入")
        await manager.close()
        self.assertTrue(storage.closed)

    async def test_close_waits_for_active_snapshot_write(self) -> None:
        storage = DelayedStorage()
        manager = SessionManager(storage=storage)
        append_task = asyncio.create_task(manager.append_and_persist(
            "session", ConversationTurn("输入", "回复", time.time())
        ))
        await storage.write_started.wait()
        close_task = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())
        storage.allow_write.set()
        await asyncio.gather(append_task, close_task)
        self.assertTrue(storage.closed)


class SessionPersistenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(
        self, data_dir: Path, context: FakeContext, **overrides
    ) -> AliceAgentPlugin:
        config = {
            "session_persistence_enabled": True,
            "history_restore_max_age_hours": 72,
            "daily_events_enabled": False,
            "memory_enabled": False,
            "memory_backend": "in_memory",
            "vad_enabled": False,
        }
        config.update(overrides)
        with patch.object(plugin_main.StarTools, "get_data_dir", return_value=data_dir):
            return AliceAgentPlugin(context, config)

    @staticmethod
    def _agent_event(text: str, message_id: str):
        event = FakeEvent(text)
        event.message_obj.message_id = message_id
        event.message_obj.timestamp = time.time()
        return AstrBotMessageAdapter().to_agent_event(event)

    @staticmethod
    async def _still_current() -> bool:
        return True

    async def test_reload_uses_only_committed_partial_text_and_keeps_mood_without_daily_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            context1 = FakeContext()
            first = self._plugin(data_dir, context1)
            uncommitted = self._agent_event("未提交的首轮用户内容", "uncommitted")
            generated = await first._agent.handle_event(
                uncommitted, is_current=self._still_current
            )
            self.assertEqual(generated.text, "你好，我在。")
            await first.terminate()

            context2 = FakeContext()
            second = self._plugin(data_dir, context2)
            partial_event = self._agent_event("已经部分发送的一轮", "partial")
            partial_response = await second._agent.handle_event(
                partial_event, is_current=self._still_current
            )
            await second._agent.commit_delivery(
                partial_event, partial_response, "实际发送部分"
            )
            await second._agent._pipeline._state_manager.commit_update(
                partial_event.session_id,
                AgentState(mood_valence=0.37, mood_arousal=0.62, energy=0.54),
            )
            self.assertFalse(second._config.daily_events_enabled)
            await second.terminate()

            context3 = FakeContext()
            third = self._plugin(data_dir, context3)
            next_event = self._agent_event("继续聊", "next")
            await third._agent.handle_event(
                next_event, is_current=self._still_current
            )
            self.assertEqual(len(context3.llm_calls), 3)
            prompts = [call["prompt"] for call in context3.llm_calls]
            perception = json.loads(prompts[0])
            response = json.loads(prompts[-1])
            for payload in (perception, response):
                history = payload["recent_history"]
                self.assertEqual(history[-1]["user_text"], "已经部分发送的一轮")
                self.assertEqual(history[-1]["assistant_text"], "实际发送部分")
                self.assertNotIn("我在。", json.dumps(history, ensure_ascii=False))
                self.assertNotIn("未提交的首轮用户内容", json.dumps(payload, ensure_ascii=False))
            state = await third._agent._pipeline._state_manager.get(partial_event.session_id)
            self.assertEqual(state.mood_valence, 0.37)
            self.assertEqual(state.mood_arousal, 0.62)
            await third.terminate()

    async def test_disabled_persistence_does_not_attach_to_existing_history_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            seed = SessionManager(storage=SQLiteStorageAdapter(data_dir / "alice_sessions.db"))
            await seed.append_and_persist(
                "bot-id:FriendMessage:user-1",
                ConversationTurn("旧历史", "旧回复", time.time()),
            )
            await seed.close()

            with patch.object(plugin_main.StarTools, "get_data_dir", return_value=data_dir):
                plugin = AliceAgentPlugin(FakeContext(), {
                    "session_persistence_enabled": False,
                    "daily_events_enabled": False,
                    "memory_enabled": False,
                    "memory_backend": "in_memory",
                    "vad_enabled": False,
                })
            self.assertIsNone(plugin._session_manager._storage)
            self.assertEqual(
                await plugin._session_manager.load_history("bot-id:FriendMessage:user-1"),
                [],
            )
            await plugin.terminate()

    async def test_cancelled_segment_delivery_persists_only_yielded_text(self) -> None:
        class TwoSentenceContext(FakeContext):
            async def llm_generate(self, **kwargs):
                result = await super().llm_generate(**kwargs)
                if not kwargs["system_prompt"].startswith((
                    "你是 Alice Agent 的 Perception",
                    "你是 Alice Agent 的 Planner",
                )):
                    result.completion_text = "已发出的第一句。未发送的回复后缀。"
                return result

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            context = TwoSentenceContext()
            plugin = self._plugin(
                data_dir,
                context,
                response_timing_enabled=False,
                segmented_send_enabled=True,
                segmented_send_interval_seconds=30.0,
            )
            plugin._agent_loop.on_user_event = lambda _event: asyncio.sleep(0, result=True)
            event = FakeEvent("请分段回复")
            event.message_obj.message_id = "partial-delivery"
            event.message_obj.timestamp = time.time()
            first_yield = asyncio.Event()
            yielded: list[str] = []

            async def consume() -> None:
                async for result in plugin.on_private_message(event):
                    yielded.append(result[1])
                    if len(yielded) == 1:
                        first_yield.set()

            consumer = asyncio.create_task(consume())
            await asyncio.wait_for(first_yield.wait(), timeout=3.0)
            await asyncio.sleep(0.05)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assertEqual(yielded, ["已发出的第一句。"])
            session_id = event.unified_msg_origin
            await plugin.terminate()

            reloaded = self._plugin(data_dir, FakeContext())
            history = await reloaded._session_manager.load_history(session_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].assistant_text, "已发出的第一句。")
            self.assertNotIn("未发送的回复后缀", history[0].assistant_text or "")
            await reloaded.terminate()

