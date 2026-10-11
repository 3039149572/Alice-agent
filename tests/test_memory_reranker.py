from __future__ import annotations

import unittest

from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryItem, MemoryQuery
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.memory.manager import MemoryManager
from astrbot_plugin_alice_agent.alice_agent.memory.retriever import MemoryRetriever
from astrbot_plugin_alice_agent.alice_agent.memory.writer import MemoryWriter
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState


def query() -> MemoryQuery:
    return MemoryQuery(
        "s", "u", "终于考完了", "考试", PerceptionResult(topic="考试"),
        AgentState(), 5,
    )


class MemoryRerankerTests(unittest.TestCase):
    def test_high_importance_cannot_bypass_relevance_gate(self) -> None:
        relevant = MemoryItem(
            "a", "正在准备考试", retrieval_score=0.8, importance=0.4,
        )
        irrelevant = MemoryItem(
            "b", "第一次完成项目", retrieval_score=0.05,
            importance=1.0, pinned=True,
        )
        result = MemoryReranker().rerank([irrelevant, relevant], 5, query())
        self.assertEqual([item.id for item in result.selected], ["a"])
        rejected = next(score for score in result.scores if score.memory_id == "b")
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "relevance below threshold")

    def test_importance_breaks_near_relevance_tie(self) -> None:
        lower_importance = MemoryItem(
            "a", "考试 A", retrieval_score=0.60, importance=0.1,
        )
        higher_importance = MemoryItem(
            "b", "考试 B", retrieval_score=0.58, importance=0.95,
        )
        result = MemoryReranker().rerank(
            [lower_importance, higher_importance], 5, query()
        )
        self.assertEqual(result.selected[0].id, "b")


class MemoryRerankerDebugTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_log_explains_scores_and_rejection(self) -> None:
        class CandidatePort:
            async def recall(self, _query):
                return [
                    MemoryItem("accepted", "考试", retrieval_score=0.8),
                    MemoryItem("rejected", "项目", retrieval_score=0.01, importance=1.0),
                ]

            async def observe(self, _observation): return False
            async def update(self, _item): return None
            async def get(self, _memory_id): return None
            async def close(self): return None

        class Logger:
            def __init__(self): self.messages = []
            def debug(self, message, *args): self.messages.append(str(message))
            def info(self, message, *args): self.messages.append(str(message))
            def warning(self, message, *args): self.messages.append(str(message))
            def error(self, message, *args): self.messages.append(str(message))
            def exception(self, message, *args): self.messages.append(str(message))

        port = CandidatePort()
        logger = Logger()
        manager = MemoryManager(
            port, MemoryRetriever(port, 5), MemoryWriter(port, 0.6),
            MemoryReranker(), 5, logger=logger, debug=True,
        )
        await manager.recall(
            AgentEvent("e", "trace", "private", "test", "s", "u", 1.0, "考试"),
            PerceptionResult(topic="考试"), AgentState(), RelationshipState("u"),
        )
        output = "\n".join(logger.messages)
        self.assertIn("retrieval=0.800", output)
        self.assertIn("final=", output)
        self.assertIn("relevance below threshold", output)


if __name__ == "__main__":
    unittest.main()
