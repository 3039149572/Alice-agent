from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_alice_agent.adapters.astrbot.llm_adapter import AstrBotLLMAdapter
from astrbot_plugin_alice_agent.adapters.astrbot.message_adapter import AstrBotMessageAdapter
from astrbot_plugin_alice_agent.adapters.astrbot.message_filter import AlicePrivateMessageFilter
from astrbot_plugin_alice_agent.adapters.astrbot.media_renderer import RenderedMedia
from astrbot_plugin_alice_agent.adapters.diagnostics import AtomicJSONTurnLogger
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMRequest
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.main import AliceAgentPlugin
from astrbot_plugin_alice_agent import main as plugin_main
from astrbot.core.star.star_handler import star_handlers_registry


class FakeContext:
    def __init__(self) -> None:
        self.provider_sessions: list[str] = []
        self.llm_calls: list[dict[str, str]] = []

    async def get_current_chat_provider_id(self, session_id: str) -> str:
        self.provider_sessions.append(session_id)
        return "provider-current"

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        system = kwargs["system_prompt"]
        if system.startswith("你是 Alice Agent 的 Perception"):
            text = json.dumps(
                {
                    "intent": "chat",
                    "topic": "greeting",
                    "user_emotion": "neutral",
                    "valence": 0,
                    "arousal": 0.2,
                    "social_signals": {},
                    "importance": 0.1,
                    "memory_candidate": False,
                    "confidence": 0.8,
                }
            )
        elif system.startswith("你是 Alice Agent 的 Planner"):
            text = json.dumps(
                {
                    "action": "reply",
                    "intent": "greet",
                    "response_goal": "回应问候",
                    "emotion": "calm",
                    "energy": 0.5,
                    "memory_ids": [],
                    "expression": None,
                    "voice_style": None,
                }
            )
        else:
            text = "你好，我在。"
        return SimpleNamespace(completion_text=text, result_chain=None)


class FakeEvent:
    def __init__(self, text: str = "你好") -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id="message-1", message_str=text, timestamp=1
        )
        self.unified_msg_origin = "bot-id:FriendMessage:user-1"
        self.stopped = False
        self.sent = []

    def get_message_type(self):
        return SimpleNamespace(value="FriendMessage")

    def get_platform_id(self):
        return "bot-id"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_name(self):
        return "Tester"

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "user-1"

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


class FakeMediaRenderer:
    async def initialize(self):
        return None

    async def render(self, session_id, response_text, decision):
        return RenderedMedia(audio_path="C:/temp/alice.wav")

    async def close(self):
        return None


class AstrBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir_patch = patch.object(plugin_main.StarTools, "get_data_dir", return_value=Path(temporary.name))
        data_dir_patch.start()
        self.addCleanup(data_dir_patch.stop)

    async def test_plugin_initialize_and_terminate_manage_single_loop(self) -> None:
        plugin = AliceAgentPlugin(
            FakeContext(), {"memory_enabled": False, "memory_backend": "in_memory", "vad_enabled": False,
                           "session_persistence_enabled": False}
        )
        await plugin.initialize()
        task = plugin._agent_loop._task
        await plugin.initialize()
        self.assertIs(plugin._agent_loop._task, task)
        self.assertTrue(plugin._agent_loop.running)
        await plugin.terminate()
        self.assertFalse(plugin._agent_loop.running)

    async def test_handler_registration_keeps_priority_and_both_filters(self) -> None:
        handlers = [
            item
            for item in star_handlers_registry
            if item.handler_module_path == plugin_main.__name__
            and item.handler_name == "on_private_message"
        ]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].extras_configs.get("priority"), 300000)
        self.assertEqual(len(handlers[0].event_filters), 2)

    async def test_llm_adapter_uses_current_session_provider(self) -> None:
        context = FakeContext()
        result = await AstrBotLLMAdapter(context).generate(
            LLMRequest("response", "umo-1", "prompt", "system")
        )
        self.assertEqual(context.provider_sessions, ["umo-1"])
        self.assertEqual(context.llm_calls[0]["chat_provider_id"], "provider-current")
        self.assertEqual(result.text, "你好，我在。")

    async def test_message_adapter_keeps_user_and_session_separate(self) -> None:
        mapped = AstrBotMessageAdapter().to_agent_event(FakeEvent())
        self.assertEqual(mapped.session_id, "bot-id:FriendMessage:user-1")
        self.assertEqual(mapped.user_id, "user-1")
        self.assertNotEqual(mapped.session_id, mapped.user_id)
        self.assertTrue(mapped.trace_id.startswith("alice_19700101_"))

    async def test_handler_yields_before_stopping_default_pipeline(self) -> None:
        event = FakeEvent()
        plugin = AliceAgentPlugin(
            FakeContext(), {"memory_enabled": False, "memory_backend": "in_memory", "vad_enabled": False,
                           "session_persistence_enabled": False}
        )
        generator = plugin.on_private_message(event)
        yielded = await anext(generator)
        self.assertEqual(yielded, ("plain", "你好，我在。"))
        self.assertFalse(event.stopped)
        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_handler_emits_record_and_text_chain_when_tts_succeeds(self) -> None:
        event = FakeEvent()
        plugin = AliceAgentPlugin(
            FakeContext(), {"memory_enabled": False, "memory_backend": "in_memory", "vad_enabled": False,
                           "session_persistence_enabled": False}
        )
        await plugin._media_renderer.close()
        plugin._media_renderer = FakeMediaRenderer()
        generator = plugin.on_private_message(event)
        kind, chain = await anext(generator)
        self.assertEqual(kind, "chain")
        self.assertEqual(chain[0].__class__.__name__, "Record")
        self.assertEqual(chain[1].__class__.__name__, "Plain")
        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)
        await plugin.terminate()

    async def test_test_log_switch_is_live_default_off_and_preserves_old_records(self) -> None:
        config = {
            "memory_enabled": False,
            "memory_backend": "in_memory",
            "vad_enabled": False,
            "session_persistence_enabled": False,
            "jev_api_key": "never-write-this-secret",
        }
        plugin = AliceAgentPlugin(FakeContext(), config)
        with tempfile.TemporaryDirectory() as temporary:
            turn_dir = Path(temporary) / "turns"
            plugin._test_logger = AtomicJSONTurnLogger(turn_dir)
            try:
                first = FakeEvent()
                async for _ in plugin.on_private_message(first):
                    pass
                self.assertFalse(turn_dir.exists())

                config["test_log_enabled"] = True
                second = FakeEvent()
                second.message_obj.message_id = "message-2"
                async for _ in plugin.on_private_message(second):
                    pass
                records = list(turn_dir.glob("*.json"))
                self.assertEqual(len(records), 1)
                raw = records[0].read_text(encoding="utf-8")
                saved = json.loads(raw)
                self.assertEqual(saved["user_text"], "你好")
                self.assertEqual(saved["assistant_text"], "你好，我在。")
                self.assertIn("score_sets", raw)
                self.assertIn("decision_comparison", saved)
                self.assertEqual(
                    set(saved["decision_comparison"]), {"perception", "planner"}
                )
                self.assertNotIn("never-write-this-secret", raw)

                config["test_log_enabled"] = False
                third = FakeEvent()
                third.message_obj.message_id = "message-3"
                async for _ in plugin.on_private_message(third):
                    pass
                self.assertEqual(len(list(turn_dir.glob("*.json"))), 1)
            finally:
                await plugin.terminate()

    async def test_turn_json_persists_both_decisions_when_available(self) -> None:
        plugin = AliceAgentPlugin(
            FakeContext(),
            {"memory_enabled": False, "memory_backend": "in_memory", "vad_enabled": False,
             "test_log_enabled": True, "session_persistence_enabled": False},
        )
        with tempfile.TemporaryDirectory() as temporary:
            turn_dir = Path(temporary) / "turns"
            plugin._test_logger = AtomicJSONTurnLogger(turn_dir)
            try:
                agent_event = plugin._message_adapter.to_agent_event(FakeEvent())
                comparison = {
                    "perception": {"jev": {"intent": "chat"}, "llm": {"intent": "ask"}},
                    "planner": {"jev": {"action": "comfort"}, "llm": {"action": "reply"}},
                }
                response = AgentResponse(
                    "完整回复。", agent_event.trace_id,
                    metadata={"turn_diagnostics": {"decision_comparison": comparison}},
                )
                await plugin._record_test_turn(
                    agent_event, response,
                    {"yielded_text_segments": ["完整回复。"], "status": "handed_to_framework"},
                )
                records = list(turn_dir.glob("*.json"))
                self.assertEqual(len(records), 1)
                saved = json.loads(records[0].read_text(encoding="utf-8"))
                self.assertEqual(saved["decision_comparison"], comparison)
            finally:
                await plugin.terminate()

    async def test_filter_preserves_wake_prefix_commands(self) -> None:
        message_filter = AlicePrivateMessageFilter(False)
        self.assertFalse(message_filter.filter(FakeEvent("/reset"), {"wake_prefix": ["/"]}))
        self.assertTrue(message_filter.filter(FakeEvent("普通聊天"), {"wake_prefix": ["/"]}))


if __name__ == "__main__":
    unittest.main()
