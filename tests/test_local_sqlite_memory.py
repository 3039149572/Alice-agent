from __future__ import annotations

import ast
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.memory.local_sqlite import (
    LocalSQLiteMemoryAdapter,
    LocalSQLiteMemoryConfig,
)
from astrbot_plugin_alice_agent.alice_agent.domain.memory import (
    MemoryItem,
    MemoryObservation,
    MemoryQuery,
)
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState


def item(memory_id: str, *, user_id: str = "user-a", session_id: str = "session-a", content: str = "我最喜欢蓝色") -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        content=content,
        memory_type="conversation_event",
        importance=0.9,
        emotional_weight=0.4,
        relationship_weight=0.2,
        confidence=0.9,
        created_at=time.time(),
        metadata={
            "user_id": user_id,
            "session_id": session_id,
            "trace_id": f"trace-{memory_id}",
            "topic": "颜色偏好",
        },
    )


def query(*, user_id: str = "user-a", session_id: str = "session-a", text: str = "你还记得我喜欢什么颜色吗") -> MemoryQuery:
    return MemoryQuery(
        session_id=session_id,
        user_id=user_id,
        text=text,
        topic="颜色偏好",
        perception=PerceptionResult(topic="颜色偏好"),
        state=AgentState(),
        limit=5,
    )


class LocalSQLiteMemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_memory_adapter_tree_has_no_network_imports(self) -> None:
        adapter_dir = Path(__file__).parents[1] / "adapters" / "memory"
        forbidden = {"httpx", "requests", "aiohttp", "urllib", "socket"}
        imported: set[str] = set()
        for source_path in adapter_dir.glob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
        self.assertFalse(imported & forbidden)

    async def test_candidate_persists_across_adapter_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(path))
            await adapter.observe(MemoryObservation(item("m1"), "我记住了", True))
            await adapter.close()
            reopened = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(path))
            recalled = await reopened.recall(query())
            self.assertEqual([memory.id for memory in recalled], ["m1"])
            self.assertIsNone(recalled[0].last_accessed_at)
            self.assertIsNotNone(recalled[0].retrieval_score)
            await reopened.close()

    async def test_non_candidate_is_journaled_but_not_recallable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(Path(directory) / "memory.db"))
            accepted = await adapter.observe(MemoryObservation(item("m1"), "普通回复", False))
            self.assertTrue(accepted)
            self.assertEqual(await adapter.counts(), (0, 1))
            self.assertEqual(await adapter.recall(query()), [])
            await adapter.close()

    async def test_user_scope_recalls_across_sessions_but_not_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(Path(directory) / "memory.db", scope="user"))
            await adapter.observe(MemoryObservation(item("m1"), "好的", True))
            self.assertEqual([memory.id for memory in await adapter.recall(query(session_id="session-b"))], ["m1"])
            self.assertEqual(await adapter.recall(query(user_id="user-b")), [])
            await adapter.close()

    async def test_session_scope_is_strictly_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(Path(directory) / "memory.db", scope="session"))
            await adapter.observe(MemoryObservation(item("m1"), "好的", True))
            self.assertEqual(await adapter.recall(query(session_id="session-b")), [])
            self.assertEqual([memory.id for memory in await adapter.recall(query())], ["m1"])
            await adapter.close()

    async def test_update_and_get_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(Path(directory) / "memory.db"))
            saved = item("m1")
            await adapter.observe(MemoryObservation(saved, "好的", True))
            saved.content = "我最喜欢绿色"
            await adapter.update(saved)
            loaded = await adapter.get("m1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.content, "我最喜欢绿色")
            with self.assertRaises(KeyError):
                await adapter.update(item("missing"))
            await adapter.close()

    async def test_irrelevant_memory_is_not_injected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(Path(directory) / "memory.db"))
            await adapter.observe(MemoryObservation(item("m1", content="我喜欢吃拉面"), "好的", True))
            unrelated = query(text="今天的天气怎么样")
            unrelated.topic = "天气"
            self.assertEqual(await adapter.recall(unrelated), [])
            await adapter.close()


if __name__ == "__main__":
    unittest.main()
