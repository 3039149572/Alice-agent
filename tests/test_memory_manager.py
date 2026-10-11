from __future__ import annotations

import unittest

from astrbot_plugin_alice_agent.adapters.memory.in_memory import InMemoryMemoryAdapter
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.memory.manager import MemoryManager
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.memory.retriever import MemoryRetriever
from astrbot_plugin_alice_agent.alice_agent.memory.writer import MemoryWriter


def event(session: str = "s1", text: str = "我喜欢蓝色") -> AgentEvent:
    return AgentEvent("e", "trace", "private", "test", session, "u", 1.0, text)


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = InMemoryMemoryAdapter()
        self.manager = MemoryManager(
            self.adapter,
            MemoryRetriever(self.adapter, 5),
            MemoryWriter(self.adapter, 0.6),
            MemoryReranker(),
            5,
        )
        self.plan = ActionPlan()
        self.response = AgentResponse("知道了", "trace")

    async def test_non_candidate_is_not_saved(self) -> None:
        saved = await self.manager.observe(
            event(), PerceptionResult(memory_candidate=False, importance=1.0), AgentState(), self.plan, self.response
        )
        self.assertFalse(saved)
        self.assertEqual(await self.adapter.count(), 0)

    async def test_candidate_above_threshold_is_saved(self) -> None:
        perception = PerceptionResult(
            topic="颜色偏好", memory_candidate=True, importance=0.8, confidence=0.9
        )
        saved = await self.manager.observe(event(), perception, AgentState(), self.plan, self.response)
        self.assertTrue(saved)
        self.assertEqual(await self.adapter.count(), 1)
        recalled = await self.manager.recall(event(text="蓝色怎么样"), perception, AgentState())
        self.assertEqual(len(recalled), 1)

    async def test_recall_is_session_isolated(self) -> None:
        perception = PerceptionResult(topic="颜色", memory_candidate=True, importance=0.8)
        await self.manager.observe(event("a"), perception, AgentState(), self.plan, self.response)
        recalled = await self.manager.recall(event("b", "蓝色"), perception, AgentState())
        self.assertEqual(recalled, [])


if __name__ == "__main__":
    unittest.main()
