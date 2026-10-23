from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_alice_agent.adapters.astrbot.llm_adapter import AstrBotLLMAdapter
from astrbot_plugin_alice_agent.adapters.astrbot.media_renderer import AstrBotMediaRenderer, RenderedMedia
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.core.pipeline import AgentPipeline
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMRequest, LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.llm.service import LLMService
from astrbot_plugin_alice_agent.alice_agent.media.models import MediaConfig, MediaDecision
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.runtime import turn_budget
from astrbot_plugin_alice_agent.tests.test_llm_fallback import FallbackContext, response
from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin, _plain_text


class ExecutionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_calls_share_one_turn_deadline(self):
        class SlowModel:
            async def generate(self, request):
                await asyncio.sleep(0.08)
                return LLMResult(text="ok")
        service = LLMService(SlowModel(), timeout=30)
        started = time.monotonic()
        with turn_budget(started + 0.12) as budget:
            await service.generate(LLMRequest("perception", "s", "private", "system"))
            with self.assertRaises(TimeoutError):
                await service.generate(LLMRequest("planner", "s", "private", "system"))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIn("perception_llm", budget.latency_ms)
        self.assertIn("planner_llm", budget.latency_ms)

    async def test_pipeline_budget_stops_slow_stage_and_returns_fallback(self):
        cancelled = asyncio.Event()
        async def blocked(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        pipeline = AgentPipeline(
            config=AgentConfig(turn_timeout=0.06), perception=SimpleNamespace(analyze=blocked),
            state_manager=SimpleNamespace(get=blocked), memory=None, planner=None, response_generator=None,
            session_manager=SessionManager(), logger=NullLogger(),
        )
        event = AgentEvent("e", "t", "private", "mock", "s", "u", 1.0, "hello")
        result = await asyncio.wait_for(pipeline.handle_event(event), 0.3)
        self.assertTrue(result.metadata["turn_budget_exhausted"])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(pipeline._session_manager.history("s"), [])
        self.assertIn("generation_total", result.metadata["turn_diagnostics"]["stage_latency_ms"])

    async def test_hung_primary_leaves_time_for_working_backup(self):
        context = FallbackContext({}, ["backup"])
        calls, cancelled = [], asyncio.Event()
        async def generate(**kwargs):
            provider = kwargs["chat_provider_id"]
            calls.append(provider)
            if provider == "primary":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return response("backup worked")
        context.llm_generate = generate
        adapter = AstrBotLLMAdapter(context, attempt_timeout=0.04, fallback_reserve=0.04)
        request = LLMRequest("response", "session", "private", "secret", timeout_seconds=0.2)
        result = await asyncio.wait_for(adapter.generate(request), 0.3)
        self.assertEqual(result.text, "backup worked")
        self.assertEqual(calls, ["primary", "backup"])
        self.assertTrue(cancelled.is_set())
        records = adapter.attempt_snapshot()
        self.assertEqual([row["status"] for row in records], ["timeout", "ok"])
        self.assertNotIn("private", str(records))
        self.assertNotIn("secret", str(records))

    async def test_text_is_yielded_before_blocked_media_and_cancel_stops_render(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        class SlowRenderer:
            async def render(self, *args):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            async def close(self):
                pass
        plugin = _make_plugin(
            self, lambda _event: AgentResponse("文字先发。", "t"),
            media_text_first=True, segmented_send_interval_seconds=0,
            media_render_timeout_seconds=0.06,
        )
        plugin._media_renderer = SlowRenderer()
        stream = plugin.on_private_message(FakeEvent())
        first = await asyncio.wait_for(anext(stream), 0.2)
        self.assertEqual(_plain_text(first), "文字先发。")
        self.assertEqual([item async for item in stream], [])
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.is_set())

    async def test_direct_media_render_has_its_own_budget(self):
        class BlockedModel:
            async def generate(self, request):
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as folder:
            renderer = AstrBotMediaRenderer(
                MediaConfig(enabled=True, character_name="alice", onnx_model_dir="model", render_timeout_seconds=0.05),
                BlockedModel(), NullLogger(), Path(folder),
            )
            result = await asyncio.wait_for(renderer.render("s", "long enough", MediaDecision(send_voice=True)), 0.3)
            self.assertTrue(result.timed_out)
            self.assertIsNone(result.audio_path)
            await renderer.close()

    async def test_closing_partial_text_delivery_preserves_only_sent_prefix(self):
        plugin = _make_plugin(
            self, lambda _event: AgentResponse("unused", "t"),
            media_text_first=True, segmented_send_interval_seconds=0,
        )
        plugin._is_batch_current = lambda *args: True
        framework = FakeEvent()
        event = plugin._message_adapter.to_agent_event(framework)
        delivery = {"yielded_text_segments": [], "segments_yielded": 0}
        stream = plugin._deliver_response_locked(
            framework, event, AgentResponse("第一句。第二句。", "t"),
            SimpleNamespace(phase="ready"), 1, 1, delivery, {},
        )
        self.assertEqual(_plain_text(await anext(stream)), "第一句。")
        await stream.aclose()
        history = plugin._session_manager.history(event.session_id)
        self.assertEqual(history[-1].assistant_text, "第一句。")
