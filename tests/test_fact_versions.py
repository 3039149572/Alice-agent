from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.memory.local_sqlite import LocalSQLiteMemoryAdapter, LocalSQLiteMemoryConfig
from astrbot_plugin_alice_agent.alice_agent.memory.writer import MemoryWriter
from astrbot_plugin_alice_agent.alice_agent.memory.facts import extract_explicit_facts
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryQuery
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState


class FactVersionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "memory.db"
        self.adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(self.path))
        self.addAsyncCleanup(self.adapter.close)
        self.writer = MemoryWriter(self.adapter, 0.6, fact_extraction_enabled=True)

    async def observe(self, text, timestamp, user="u", trace=None):
        trace = trace or f"trace_{user}_{timestamp}"
        event = AgentEvent(trace, trace, "private", "mock", f"session_{user}", user, timestamp, text)
        observation = self.writer.build_observation(
            event, PerceptionResult(), AgentState(), ActionPlan(), AgentResponse("ok", trace),
            RelationshipState(user_id=user),
        )
        await self.adapter.observe(observation)
        return observation

    async def recall(self, text, user="u"):
        return await self.adapter.recall(MemoryQuery(
            f"session_{user}", user, text, "general", PerceptionResult(), AgentState(), 5,
        ))

    async def test_repetition_provenance_and_correction_are_versioned(self):
        first = await self.observe("我现在住在北京。", 1)
        repeat = await self.observe("我现在住在北京。", 2)
        newest = await self.observe("我现在住在上海。", 3)
        self.assertEqual(first.persisted_items[0].id, repeat.persisted_items[0].id)
        self.assertNotEqual(first.persisted_items[0].id, newest.persisted_items[0].id)
        self.assertEqual(await self.adapter.counts(), (2, 3))
        recalled = await self.recall("我现在住哪里")
        self.assertEqual([item.metadata["fact_value"] for item in recalled], ["上海"])
        old = await self.adapter.get(first.persisted_items[0].id)
        self.assertEqual(old.metadata["fact_status"], "superseded")
        self.assertEqual(old.metadata["valid_until"], 3)
        sources = self.adapter._connection.execute("SELECT COUNT(*) FROM memory_fact_sources").fetchone()[0]
        self.assertEqual(sources, 3)

    async def test_old_arriving_statement_cannot_replace_current_fact_and_users_are_isolated(self):
        await self.observe("我现在住在上海。", 3)
        await self.observe("我现在住在北京。", 1)
        await self.observe("我现在住在杭州。", 4, user="other")
        self.assertEqual([item.metadata["fact_value"] for item in await self.recall("我现在住哪里")], ["上海"])
        self.assertEqual([item.metadata["fact_value"] for item in await self.recall("我现在住哪里", "other")], ["杭州"])

    async def test_uncertain_quoted_historical_and_third_person_statements_abstain(self):
        for text in ("如果我住在北京。", "我以前住在北京。", "他说我住在北京。", "我住在北京吗？",
                     "我可能对花生过敏。", "小说里我叫小明。", "我说‘我叫小明’。"):
            with self.subTest(text=text):
                self.assertEqual(extract_explicit_facts(text), [])
        self.assertEqual(extract_explicit_facts("我现在对花生不再过敏了。")[0].value, "no")

    async def test_restart_preserves_current_fact_and_exact_original_journal(self):
        text = "我叫小明。我现在住在上海。"
        await self.observe(text, 1)
        await self.adapter.close()
        self.adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(self.path))
        self.addAsyncCleanup(self.adapter.close)
        self.assertEqual([item.metadata["fact_value"] for item in await self.recall("我叫什么名字")], ["小明"])
        row = self.adapter._connection.execute("SELECT user_text FROM conversation_turns").fetchone()
        self.assertEqual(row[0], text)
        self.assertEqual(await self.adapter.counts(), (2, 1))

    async def test_failed_fact_write_rolls_back_journal_and_all_claims(self):
        real_upsert = self.adapter._facts.upsert
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated write failure")
            return real_upsert(*args, **kwargs)
        self.adapter._facts.upsert = fail_second
        with self.assertRaises(RuntimeError):
            await self.observe("我叫小明。我现在住在上海。", 1)
        self.assertEqual(await self.adapter.counts(), (0, 0))
        self.assertEqual(self.adapter._connection.execute("SELECT COUNT(*) FROM memory_fact_versions").fetchone()[0], 0)
