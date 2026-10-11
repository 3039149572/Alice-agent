from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeEvent
from astrbot_plugin_alice_agent.tests.test_reactive_segments import _make_plugin, _plain_text


def _event(text: str, message_id: str) -> FakeEvent:
    event = FakeEvent(text)
    event.message_obj.message_id = message_id
    return event


def _stub_loop_input_registration(plugin) -> None:
    revisions = 0

    async def register(_event):
        nonlocal revisions
        revisions += 1
        return revisions

    plugin._agent_loop.register_user_event = register
    plugin._agent_loop.apply_user_activity = AsyncMock()


async def _collect(plugin, event):
    return [result async for result in plugin.on_private_message(event)]


class ReactiveTimingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_question_skips_quiet_window(self) -> None:
        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse("我在。", "trace-question"),
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.4,
            response_wait_max_seconds=0.6,
            segmented_send_interval_seconds=0,
        )
        _stub_loop_input_registration(plugin)
        started = time.monotonic()
        yielded = await asyncio.wait_for(
            _collect(plugin, _event("在吗", "question-1")), timeout=0.25
        )
        self.assertEqual([_plain_text(item) for item in yielded], ["我在。"])
        self.assertLess(time.monotonic() - started, 0.25)

    async def test_two_quick_text_messages_are_one_generation_with_boundaries(self) -> None:
        received = []

        def reply(agent_event):
            received.append(agent_event)
            return AgentResponse("合并回答。", "trace-merged")

        plugin = _make_plugin(
            self,
            reply,
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.08,
            response_wait_max_seconds=0.4,
            segmented_send_interval_seconds=0,
            test_log_enabled=True,
        )
        _stub_loop_input_registration(plugin)
        owner = _event("我今天遇到一件事", "merge-1")
        continuation = _event("後來又想到一點", "merge-2")
        owner_task = asyncio.create_task(_collect(plugin, owner))
        await asyncio.sleep(0.025)
        continuation_results = await _collect(plugin, continuation)
        owner_results = await owner_task

        self.assertEqual(continuation_results, [])
        self.assertEqual([_plain_text(item) for item in owner_results], ["合并回答。"])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].text, "我今天遇到一件事\n後來又想到一點")
        self.assertEqual(
            [item["text"] for item in received[0].payload["user_messages"]],
            ["我今天遇到一件事", "後來又想到一點"],
        )
        history = plugin._session_manager.history(owner.unified_msg_origin)
        self.assertEqual(history[-1].user_text, received[0].text)

        records = list((plugin._test_data_dir / "test_turns").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(len(record["user_messages"]), 2)
        self.assertGreaterEqual(record["response_timing"]["actual_wait_seconds"], 0)
        self.assertEqual(len(record["response_timing"]["decisions"]), 2)

    async def test_new_message_during_generation_invalidates_unsent_reply_then_retries_batch(self) -> None:
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()
        received = []

        async def reply(agent_event, is_current=None):
            received.append(agent_event)
            if len(received) == 1:
                generation_started.set()
                await release_generation.wait()
                return AgentResponse("过期生成。", "trace-stale")
            return AgentResponse("保留两句后的回答。", "trace-current")

        plugin = _make_plugin(
            self,
            reply,
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.015,
            response_wait_max_seconds=0.3,
            segmented_send_interval_seconds=0,
        )
        _stub_loop_input_registration(plugin)
        first = _event("第一段输入", "generation-1")
        second = _event("第二段补充", "generation-2")
        first_task = asyncio.create_task(_collect(plugin, first))
        await asyncio.wait_for(generation_started.wait(), timeout=0.2)
        send_lock = plugin._send_coordinator.lock_for(first.unified_msg_origin)
        await asyncio.wait_for(send_lock.acquire(), timeout=0.1)
        send_lock.release()
        self.assertEqual(await _collect(plugin, second), [])
        release_generation.set()
        first_results = await asyncio.wait_for(first_task, timeout=0.8)

        self.assertEqual([_plain_text(item) for item in first_results], ["保留两句后的回答。"])
        self.assertEqual(len(received), 2)
        self.assertEqual(received[1].text, "第一段输入\n第二段补充")
        self.assertNotIn("过期生成。", [_plain_text(item) for item in first_results])
        history = plugin._session_manager.history(first.unified_msg_origin)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].user_text, "第一段输入\n第二段补充")
        self.assertEqual(history[0].assistant_text, "保留两句后的回答。")

    async def test_no_reply_during_generation_suppresses_old_unreleased_batch(self) -> None:
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()
        calls = 0

        async def reply(agent_event, is_current=None):
            nonlocal calls
            calls += 1
            generation_started.set()
            await release_generation.wait()
            return AgentResponse("这条不能发出。", "trace-cancelled")

        plugin = _make_plugin(
            self,
            reply,
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.01,
            response_wait_max_seconds=0.3,
            segmented_send_interval_seconds=0,
        )
        _stub_loop_input_registration(plugin)
        first = _event("我想补充一件事", "cancel-1")
        no_reply = _event("不用回复了", "cancel-2")
        first_task = asyncio.create_task(_collect(plugin, first))
        await asyncio.wait_for(generation_started.wait(), timeout=0.2)
        self.assertEqual(await _collect(plugin, no_reply), [])
        await asyncio.sleep(0)
        release_generation.set()

        self.assertEqual(await asyncio.wait_for(first_task, timeout=0.5), [])
        self.assertEqual(calls, 1)
        history = plugin._session_manager.history(first.unified_msg_origin)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].assistant_text)
        self.assertEqual(history[0].user_text, "我想补充一件事\n不用回复了")

    async def test_new_input_stops_remaining_segments_but_commits_only_delivered_text(self) -> None:
        generated = []

        def reply(agent_event):
            generated.append(agent_event.text)
            if len(generated) == 1:
                return AgentResponse("第一段。第二段。", "trace-segmented")
            return AgentResponse("后续单独回答。", "trace-follow-up")

        plugin = _make_plugin(
            self,
            reply,
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.01,
            response_wait_max_seconds=0.3,
            segmented_send_max_chars=6,
            segmented_send_interval_seconds=0.12,
        )
        _stub_loop_input_registration(plugin)
        first = _event("请解释这个问题？", "segment-1")
        second = _event("补充一个新点？", "segment-2")
        first_results = []
        second_task = None
        async for result in plugin.on_private_message(first):
            first_results.append(result)
            if len(first_results) == 1:
                second_task = asyncio.create_task(_collect(plugin, second))
        self.assertIsNotNone(second_task)
        second_results = await asyncio.wait_for(second_task, timeout=0.8)

        self.assertEqual([_plain_text(item) for item in first_results], ["第一段。"])
        self.assertEqual([_plain_text(item) for item in second_results], ["后续单独回答。"])
        history = plugin._session_manager.history(first.unified_msg_origin)
        self.assertEqual(history[0].assistant_text, "第一段。")
        self.assertEqual(history[1].user_text, "补充一个新点？")
        self.assertEqual(history[1].assistant_text, "后续单独回答。")

    async def test_timing_disabled_preserves_planner_silence(self) -> None:
        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse(None, "trace-silence", action="ignore"),
            response_timing_enabled=False,
            segmented_send_interval_seconds=0,
        )
        _stub_loop_input_registration(plugin)
        event = _event("这条旧路径保持安静", "silent-1")

        yielded = await _collect(plugin, event)

        self.assertEqual(yielded, [])
        self.assertTrue(event.stopped)
        history = plugin._session_manager.history(event.unified_msg_origin)
        self.assertIsNone(history[-1].assistant_text)

    async def test_slow_classification_deadline_keeps_explicit_no_reply(self) -> None:
        classification_cancelled = asyncio.Event()

        async def slow_decide(*_args, **_kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                classification_cancelled.set()
                raise

        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse("不应走到生成。", "trace-no-reply-deadline"),
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.1,
            response_wait_max_seconds=0.1,
            segmented_send_interval_seconds=0,
            test_log_enabled=True,
        )
        _stub_loop_input_registration(plugin)
        plugin._timing_service.decide = slow_decide
        event = _event("不用回复了", "deadline-no-reply-1")

        yielded = await asyncio.wait_for(_collect(plugin, event), timeout=0.6)

        self.assertEqual(yielded, [])
        self.assertTrue(event.stopped)
        await asyncio.wait_for(classification_cancelled.wait(), timeout=0.1)
        self.assertEqual(len(plugin._agent.handle_event.await_args_list), 0)
        history = plugin._session_manager.history(event.unified_msg_origin)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].user_text, "不用回复了")
        self.assertIsNone(history[0].assistant_text)
        records = list((plugin._test_data_dir / "test_turns").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["response_timing"]["decisions"][0]["rule_action"], "ignore")
        self.assertTrue(record["response_timing"]["hard_deadline_reached"])

    async def test_terminate_cancels_pending_reactive_and_classification_tasks(self) -> None:
        classification_started = asyncio.Event()
        classification_cancelled = asyncio.Event()

        async def blocked_decide(*_args, **_kwargs):
            classification_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                classification_cancelled.set()
                raise

        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse("卸载时不应生成。", "trace-unload"),
            response_timing_enabled=True,
            response_wait_quiet_seconds=0.2,
            response_wait_max_seconds=0.4,
            segmented_send_interval_seconds=0,
        )
        _stub_loop_input_registration(plugin)
        plugin._timing_service.decide = blocked_decide
        event = _event("一条等待合并的普通陈述", "unload-pending-1")
        handler = asyncio.create_task(_collect(plugin, event))
        await asyncio.wait_for(classification_started.wait(), timeout=0.3)

        await asyncio.wait_for(plugin.terminate(), timeout=0.5)
        handler_result = await asyncio.gather(handler, return_exceptions=True)

        self.assertIsInstance(handler_result[0], asyncio.CancelledError)
        self.assertTrue(classification_cancelled.is_set())
        self.assertEqual(plugin._reactive_tasks, set())
        self.assertEqual(plugin._classification_tasks, set())
        self.assertEqual(plugin._pending_replies._queues, {})


if __name__ == "__main__":
    unittest.main()
