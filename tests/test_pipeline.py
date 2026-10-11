from __future__ import annotations

import json
import unittest

from astrbot_plugin_alice_agent.adapters.memory.in_memory import InMemoryMemoryAdapter
from astrbot_plugin_alice_agent.adapters.storage.in_memory import InMemoryStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.core.pipeline import AgentPipeline
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.generation.response_generator import ResponseGenerator
from astrbot_plugin_alice_agent.alice_agent.llm.service import LLMService
from astrbot_plugin_alice_agent.alice_agent.memory.manager import MemoryManager
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.memory.retriever import MemoryRetriever
from astrbot_plugin_alice_agent.alice_agent.memory.writer import MemoryWriter
from astrbot_plugin_alice_agent.alice_agent.perception.parser import PerceptionParser
from astrbot_plugin_alice_agent.alice_agent.perception.service import PerceptionService
from astrbot_plugin_alice_agent.alice_agent.planner.parser import ActionPlanParser
from astrbot_plugin_alice_agent.alice_agent.planner.service import PlannerService
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer
from astrbot_plugin_alice_agent.tests.helpers import ScriptedLLM


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_pipeline_with_mock_llm(self) -> None:
        perception_json = json.dumps(
            {
                "intent": "complain",
                "topic": "被忽视",
                "user_emotion": "hurt",
                "valence": -0.6,
                "arousal": 0.7,
                "social_signals": {
                    "complaint": 0.8,
                    "reassurance_seeking": 0.7,
                    "vulnerability": 0.5,
                },
                "importance": 0.8,
                "memory_candidate": True,
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )
        planner_json = json.dumps(
            {
                "action": "comfort",
                "intent": "repair_connection",
                "response_goal": "承认对方被忽视的感受并真诚回应",
                "emotion": "gentle",
                "energy": 0.4,
                "memory_ids": [],
                "expression": "concerned",
                "voice_style": "soft",
            },
            ensure_ascii=False,
        )
        fake = ScriptedLLM(
            {
                "perception": perception_json,
                "planner": planner_json,
                "response": "对不起，让你有这种被晾着的感觉。我现在在认真听。",
            }
        )
        config = AgentConfig()
        llm = LLMService(fake, config.llm_timeout)
        context = ContextBuilder()
        memory_adapter = InMemoryMemoryAdapter()
        memory = MemoryManager(
            memory_adapter,
            MemoryRetriever(memory_adapter, config.memory_recall_limit),
            MemoryWriter(memory_adapter, config.memory_importance_threshold),
            MemoryReranker(),
            config.memory_recall_limit,
        )
        pipeline = AgentPipeline(
            config=config,
            perception=PerceptionService(llm, context, PerceptionParser()),
            state_manager=StateManager(InMemoryStorageAdapter(), StateReducer()),
            memory=memory,
            planner=PlannerService(llm, context, ActionPlanParser()),
            response_generator=ResponseGenerator(llm, context),
            session_manager=SessionManager(config.history_limit),
            logger=NullLogger(),
        )
        response = await pipeline.handle_event(
            AgentEvent(
                event_id="msg-1",
                trace_id="alice_test_1",
                type="private",
                source="mock",
                session_id="session-1",
                user_id="user-1",
                timestamp=1.0,
                text="你今天怎么一直不理我",
            )
        )
        self.assertEqual(fake.calls, ["perception", "planner", "response"])
        self.assertEqual(response.action, "comfort")
        self.assertIn("认真听", response.text or "")
        self.assertEqual(await memory_adapter.count(), 1)

    async def test_response_failure_returns_friendly_fallback(self) -> None:
        class FailingResponseLLM(ScriptedLLM):
            async def generate(self, request):
                if request.stage == "response":
                    raise TimeoutError("simulated")
                return await super().generate(request)

        fake = FailingResponseLLM(
            {
                "perception": '{"intent":"chat","topic":"general"}',
                "planner": '{"action":"reply"}',
            }
        )
        config = AgentConfig(fallback_text="稍等一下，我刚才走神了。")
        llm = LLMService(fake, config.llm_timeout)
        context = ContextBuilder()
        memory_adapter = InMemoryMemoryAdapter()
        pipeline = AgentPipeline(
            config=config,
            perception=PerceptionService(llm, context, PerceptionParser()),
            state_manager=StateManager(InMemoryStorageAdapter(), StateReducer()),
            memory=MemoryManager(
                memory_adapter,
                MemoryRetriever(memory_adapter, 5),
                MemoryWriter(memory_adapter, 0.6),
                MemoryReranker(),
                5,
            ),
            planner=PlannerService(llm, context, ActionPlanParser()),
            response_generator=ResponseGenerator(llm, context),
            session_manager=SessionManager(),
            logger=NullLogger(),
        )
        response = await pipeline.handle_event(
            AgentEvent("e", "trace", "private", "mock", "s", "u", 1.0, "你好")
        )
        self.assertEqual(response.text, "稍等一下，我刚才走神了。")
        self.assertTrue(response.metadata["fallback"])

    async def test_memory_failure_does_not_break_chat(self) -> None:
        class BrokenMemory:
            async def recall(self, event, perception, state):
                raise ConnectionError("simulated memory outage")

            async def observe(self, event, perception, state, plan, response):
                raise ConnectionError("simulated memory outage")

            async def close(self):
                pass

        fake = ScriptedLLM(
            {
                "perception": '{"intent":"chat","topic":"general"}',
                "planner": '{"action":"reply"}',
                "response": "即使记忆暂时不可用，我也能回应。",
            }
        )
        config = AgentConfig()
        llm = LLMService(fake, config.llm_timeout)
        context = ContextBuilder()
        pipeline = AgentPipeline(
            config=config,
            perception=PerceptionService(llm, context, PerceptionParser()),
            state_manager=StateManager(InMemoryStorageAdapter(), StateReducer()),
            memory=BrokenMemory(),
            planner=PlannerService(llm, context, ActionPlanParser()),
            response_generator=ResponseGenerator(llm, context),
            session_manager=SessionManager(),
            logger=NullLogger(),
        )
        response = await pipeline.handle_event(
            AgentEvent("e", "trace", "private", "mock", "s", "u", 1.0, "你好")
        )
        self.assertEqual(response.text, "即使记忆暂时不可用，我也能回应。")


if __name__ == "__main__":
    unittest.main()
