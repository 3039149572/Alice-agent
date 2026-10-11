from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.core.provider.provider import Provider
from astrbot.core.star.context import Context

from astrbot_plugin_alice_agent.adapters.astrbot.llm_adapter import AstrBotLLMAdapter
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMRequest
from astrbot_plugin_alice_agent.alice_agent.llm.service import LLMService


def config(fallbacks):
    return {"agent_runner": {"config": {"model": {"fallback_provider_ids": fallbacks}}}}


def response(text="备用回复", role="assistant", chain=None):
    return SimpleNamespace(completion_text=text, role=role, result_chain=chain)


class FallbackContext:
    def __init__(self, outcomes, fallbacks=()):
        self.outcomes = outcomes
        self.configs = {"session": config(list(fallbacks))}
        self.config_sessions = []
        self.calls = []

    def get_config(self, umo=None):
        self.config_sessions.append(umo)
        return self.configs.get(umo, {})

    async def get_current_chat_provider_id(self, session_id):
        return "primary"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[kwargs["chat_provider_id"]]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class LLMFallbackTests(unittest.IsolatedAsyncioTestCase):
    request = LLMRequest("response", "session", "原始提示", "原始系统提示")

    async def test_primary_success_does_not_call_backup(self):
        context = FallbackContext({"primary": response("主回复")}, ["backup"])
        result = await AstrBotLLMAdapter(context).generate(self.request)
        self.assertEqual(result.text, "主回复")
        self.assertEqual(result.model, "primary")
        self.assertEqual(len(context.calls), 1)

    async def test_exception_uses_ordered_session_backups_with_same_prompts(self):
        context = FallbackContext(
            {"primary": OSError("offline"), "first": TimeoutError(), "second": response()},
            ["first", "second"],
        )
        result = await AstrBotLLMAdapter(context).generate(self.request)
        self.assertEqual(result.text, "备用回复")
        self.assertEqual(result.model, "second")
        self.assertEqual(context.config_sessions, ["session"])
        self.assertEqual([c["chat_provider_id"] for c in context.calls], ["primary", "first", "second"])
        for call in context.calls:
            self.assertEqual(call["prompt"], self.request.prompt)
            self.assertEqual(call["system_prompt"], self.request.system_prompt)

    async def test_error_response_is_not_used_as_assistant_text(self):
        context = FallbackContext(
            {"primary": response("upstream error", "err"), "backup": response()}, ["backup"]
        )
        self.assertEqual((await AstrBotLLMAdapter(context).generate(self.request)).text, "备用回复")

    async def test_empty_or_whitespace_response_uses_backup(self):
        for text in ("", "  \n"):
            with self.subTest(text=text):
                context = FallbackContext({"primary": response(text), "backup": response()}, ["backup"])
                self.assertEqual((await AstrBotLLMAdapter(context).generate(self.request)).model, "backup")

    async def test_chain_text_is_success_without_backup(self):
        chain = SimpleNamespace(chain=[SimpleNamespace(text="链式回复"), SimpleNamespace(path="image")])
        context = FallbackContext({"primary": response("", chain=chain)}, ["backup"])
        self.assertEqual((await AstrBotLLMAdapter(context).generate(self.request)).text, "链式回复")
        self.assertEqual(len(context.calls), 1)

    async def test_deduplicates_and_ignores_invalid_provider_ids(self):
        context = FallbackContext(
            {"primary": OSError(), "first": OSError(), "second": response()},
            ["primary", "", None, 3, "first", "first", "second"],
        )
        self.assertEqual((await AstrBotLLMAdapter(context).generate(self.request)).model, "second")
        self.assertEqual([c["chat_provider_id"] for c in context.calls], ["primary", "first", "second"])

    async def test_all_failed_propagates_last_exception(self):
        error = OSError("last failure")
        context = FallbackContext({"primary": TimeoutError(), "backup": error}, ["backup"])
        with self.assertRaises(OSError) as caught:
            await AstrBotLLMAdapter(context).generate(self.request)
        self.assertIs(caught.exception, error)

    async def test_all_error_or_empty_responses_raise(self):
        for bad_response in (response("error", "err"), response("")):
            with self.subTest(response=bad_response):
                context = FallbackContext({"primary": bad_response})
                with self.assertRaises(RuntimeError):
                    await AstrBotLLMAdapter(context).generate(self.request)

    async def test_cancelled_request_never_starts_backup(self):
        context = FallbackContext({"primary": asyncio.CancelledError()}, ["backup"])
        with self.assertRaises(asyncio.CancelledError):
            await AstrBotLLMAdapter(context).generate(self.request)
        self.assertEqual(len(context.calls), 1)

    async def test_configuration_is_resolved_per_session_and_per_request(self):
        context = FallbackContext(
            {"primary": OSError(), "a": response("A"), "b": response("B")}, ["a"]
        )
        context.configs["other"] = config(["b"])
        adapter = AstrBotLLMAdapter(context)
        self.assertEqual((await adapter.generate(self.request)).text, "A")
        other = LLMRequest("perception", "other", "prompt", "system")
        self.assertEqual((await adapter.generate(other)).text, "B")
        context.configs["session"] = config(["b"])
        self.assertEqual((await adapter.generate(self.request)).text, "B")

    async def test_malformed_config_preserves_primary(self):
        for value in ({}, config("backup"), {"agent_runner": None}, config(None)):
            with self.subTest(config=value):
                context = FallbackContext({"primary": response("主回复")})
                context.configs["session"] = value
                result = await AstrBotLLMAdapter(context).generate(self.request)
                self.assertEqual(result.text, "主回复")

    async def test_legacy_context_without_get_config_still_works(self):
        context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="primary"),
            llm_generate=AsyncMock(return_value=response()),
        )
        self.assertEqual((await AstrBotLLMAdapter(context).generate(self.request)).model, "primary")

    async def test_unavailable_config_does_not_block_primary(self):
        context = FallbackContext({"primary": response()})
        context.get_config = Mock(side_effect=OSError("cannot read config"))
        self.assertEqual((await AstrBotLLMAdapter(context).generate(self.request)).model, "primary")

    async def test_total_timeout_cancels_without_starting_more_providers(self):
        context = FallbackContext({}, ["backup"])
        context.llm_generate = AsyncMock(side_effect=lambda **kwargs: None)

        async def blocked(**kwargs):
            await asyncio.Event().wait()

        context.llm_generate.side_effect = blocked
        service = LLMService(AstrBotLLMAdapter(context), timeout=1.0)
        with self.assertRaises(TimeoutError):
            await service.generate(self.request)
        self.assertEqual(context.llm_generate.await_count, 1)

    async def test_reactive_handler_uses_backup_for_perception_planner_and_response(self):
        from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeContext, FakeEvent
        from astrbot_plugin_alice_agent.main import AliceAgentPlugin

        class ReactiveContext(FakeContext):
            def get_config(self, umo=None):
                return config(["backup"])

            async def llm_generate(self, **kwargs):
                if kwargs["chat_provider_id"] == "provider-current":
                    self.llm_calls.append(kwargs)
                    raise OSError("primary unavailable")
                return await super().llm_generate(**kwargs)

        context = ReactiveContext()
        plugin = AliceAgentPlugin(context, {
            "memory_enabled": False, "memory_backend": "in_memory",
            "vad_enabled": False, "session_persistence_enabled": False,
            "response_timing_enabled": False, "daily_events_enabled": False,
            "relationship_enabled": False, "media_enabled": False,
        })
        event = FakeEvent()
        try:
            yielded = [item async for item in plugin.on_private_message(event)]
            self.assertEqual(yielded, [("plain", "你好，我在。")])
            self.assertTrue(event.stopped)
            self.assertEqual(
                [call["chat_provider_id"] for call in context.llm_calls],
                ["provider-current", "backup"] * 3,
            )
        finally:
            await plugin.terminate()

    async def test_real_astrbot_llm_generate_does_not_fallback_but_adapter_does(self):
        # Use the installed AstrBot method, mocking only actual network providers.
        primary = Mock(spec=Provider)
        primary.text_chat = AsyncMock(side_effect=OSError("offline"))
        backup = Mock(spec=Provider)
        backup.text_chat = AsyncMock(return_value=response())
        providers = {"primary": primary, "backup": backup}
        context = FallbackContext({}, ["missing-provider", "backup"])
        context.provider_manager = SimpleNamespace(
            get_provider_by_id=AsyncMock(side_effect=lambda provider_id: providers.get(provider_id))
        )
        context.llm_generate = Context.llm_generate.__get__(context)
        with self.assertRaises(OSError):
            await context.llm_generate(chat_provider_id="primary", prompt="test")
        backup.text_chat.assert_not_awaited()
        result = await AstrBotLLMAdapter(context).generate(self.request)
        self.assertEqual(result.model, "backup")
        backup.text_chat.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
