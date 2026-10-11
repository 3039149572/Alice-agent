from __future__ import annotations

import json
import unittest

from astrbot_plugin_alice_agent.adapters.memory.in_memory import InMemoryMemoryAdapter
from astrbot_plugin_alice_agent.adapters.storage.in_memory import InMemoryStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.core.pipeline import AgentPipeline
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMRequest, LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryItem, MemoryObservation
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.generation.response_generator import ResponseGenerator
from astrbot_plugin_alice_agent.alice_agent.llm.service import LLMService
from astrbot_plugin_alice_agent.alice_agent.memory.manager import MemoryManager
from astrbot_plugin_alice_agent.alice_agent.memory.metadata_store import InMemoryMetadataStore
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.memory.retriever import MemoryRetriever
from astrbot_plugin_alice_agent.alice_agent.memory.writer import MemoryWriter
from astrbot_plugin_alice_agent.alice_agent.perception.parser import PerceptionParser
from astrbot_plugin_alice_agent.alice_agent.perception.service import PerceptionService
from astrbot_plugin_alice_agent.alice_agent.planner.parser import ActionPlanParser
from astrbot_plugin_alice_agent.alice_agent.planner.service import PlannerService
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.relationship.manager import RelationshipManager
from astrbot_plugin_alice_agent.alice_agent.relationship.reducer import RelationshipReducer
from astrbot_plugin_alice_agent.alice_agent.relationship.store import InMemoryRelationshipStore
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer


class CapturingLLM:
    def __init__(self, select_memory: bool) -> None:
        self.select_memory = select_memory
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if request.stage == "perception":
            return LLMResult(json.dumps({
                "intent": "finish_exam", "topic": "考试",
                "importance": 0.5, "memory_candidate": False,
                "confidence": 0.9,
            }, ensure_ascii=False))
        if request.stage == "planner":
            return LLMResult(json.dumps({
                "action": "recall_then_reply" if self.select_memory else "reply",
                "memory_ids": ["exam"] if self.select_memory else [],
            }, ensure_ascii=False))
        return LLMResult("总算结束了，可以好好松口气了。")

    async def close(self) -> None:
        return None


class MemoryPlannerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _build(self, with_memory: bool):
        llm_adapter = CapturingLLM(select_memory=with_memory)
        llm = LLMService(llm_adapter, 5.0)
        context = ContextBuilder()
        port = InMemoryMemoryAdapter()
        if with_memory:
            await port.observe(MemoryObservation(
                MemoryItem(
                    id="exam", content="用户最近一直在准备一场重要考试",
                    importance=0.8, confidence=0.9, created_at=1.0,
                    metadata={
                        "session_id": "s", "user_id": "u", "topic": "考试",
                    },
                ),
                "之前聊过考试", True,
            ))
        metadata = InMemoryMetadataStore()
        memory = MemoryManager(
            port, MemoryRetriever(port, 5), MemoryWriter(port, 0.6),
            MemoryReranker(), 5, metadata_store=metadata,
            selected_limit=3,
        )
        relationship = RelationshipManager(
            InMemoryRelationshipStore(), RelationshipReducer(0.02)
        )
        pipeline = AgentPipeline(
            config=AgentConfig(),
            perception=PerceptionService(llm, context, PerceptionParser()),
            state_manager=StateManager(InMemoryStorageAdapter(), StateReducer()),
            memory=memory,
            planner=PlannerService(llm, context, ActionPlanParser()),
            response_generator=ResponseGenerator(llm, context),
            session_manager=SessionManager(), logger=NullLogger(),
            relationship_manager=relationship,
        )
        return pipeline, llm_adapter, metadata

    async def test_memory_flows_through_rerank_planner_and_response(self) -> None:
        pipeline, llm, metadata = await self._build(True)
        response = await pipeline.handle_event(AgentEvent(
            "e", "trace", "private", "test", "s", "u", 2.0, "终于考完了。"
        ))
        planner_payload = json.loads(
            next(request.prompt for request in llm.requests if request.stage == "planner")
        )
        response_payload = json.loads(
            next(request.prompt for request in llm.requests if request.stage == "response")
        )
        self.assertEqual(planner_payload["relevant_memories"][0]["id"], "exam")
        self.assertTrue(planner_payload["relationship"])
        self.assertEqual(response_payload["selected_memories"][0]["id"], "exam")
        self.assertEqual(response.action, "recall_then_reply")
        self.assertEqual((await metadata.get_many(["exam"]))["exam"].access_count, 1)
        await pipeline.close()

    async def test_no_memory_keeps_pipeline_normal_and_response_context_empty(self) -> None:
        pipeline, llm, _ = await self._build(False)
        response = await pipeline.handle_event(AgentEvent(
            "e", "trace", "private", "test", "s", "u", 2.0, "终于考完了。"
        ))
        response_payload = json.loads(
            next(request.prompt for request in llm.requests if request.stage == "response")
        )
        self.assertEqual(response_payload["selected_memories"], [])
        self.assertTrue(response.text)
        await pipeline.close()


if __name__ == "__main__":
    unittest.main()
