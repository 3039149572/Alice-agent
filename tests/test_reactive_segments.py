from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_alice_agent import main as plugin_main
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.adapters.astrbot.media_renderer import RenderedMedia
from astrbot_plugin_alice_agent.main import AliceAgentPlugin

from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeContext, FakeEvent


class FixedMediaRenderer:
    def __init__(self, rendered: RenderedMedia) -> None:
        self.rendered = rendered

    async def render(self, _session_id, _response_text, _decision):
        return self.rendered

    async def close(self) -> None:
        return None


class RecordingEvent(FakeEvent):
    def __init__(self, text: str, timeline: list[tuple[str, object]]) -> None:
        super().__init__(text)
        self.timeline = timeline

    async def send(self, result):
        self.sent.append(result)
        self.timeline.append(("event.send", result))


class QQOfficialEvent(RecordingEvent):
    def get_platform_name(self):
        return "qq_official"


def _plain_text(result) -> str:
    kind, payload = result
    if kind == "plain":
        return payload
    if kind == "chain":
        return "".join(
            component.text
            for component in payload
            if component.__class__.__name__ == "Plain"
        )
    raise AssertionError(f"unexpected result kind: {kind}")


def _make_plugin(
    test_case: unittest.IsolatedAsyncioTestCase,
    response_for_event,
    *,
    media: RenderedMedia | None = None,
    **config_overrides,
) -> AliceAgentPlugin:
    temporary = tempfile.TemporaryDirectory()
    test_case.addCleanup(temporary.cleanup)
    config = {
        "vad_enabled": False,
        "response_timing_enabled": False,
        "memory_enabled": False,
        "memory_backend": "in_memory",
        "session_persistence_enabled": False,
        "segmented_send_enabled": True,
        "segmented_send_max_chars": 50,
        "segmented_send_interval_seconds": 0.25,
        "media_text_first": False,  # Explicitly exercise the compatibility ordering.
        **config_overrides,
    }
    with patch.object(
        plugin_main.StarTools,
        "get_data_dir",
        return_value=Path(temporary.name),
    ):
        plugin = AliceAgentPlugin(FakeContext(), config)

    plugin._media_renderer = FixedMediaRenderer(media or RenderedMedia())
    plugin._test_data_dir = Path(temporary.name)
    plugin._agent_loop.on_user_event = AsyncMock(return_value=True)
    plugin._agent_loop.close = AsyncMock()
    async def fake_handle_event(agent_event, is_current=None):
        if is_current is not None and not await is_current():
            return AgentResponse(None, f"trace-{agent_event.trace_id}", action="superseded")
        result = response_for_event(agent_event)
        if hasattr(result, "__await__"):
            result = await result
        return result

    plugin._agent.handle_event = AsyncMock(side_effect=fake_handle_event)
    plugin._agent.close = AsyncMock()
    test_case.addAsyncCleanup(plugin.terminate)
    return plugin


class ReactiveSegmentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_closing_outer_handler_finalizes_only_the_already_yielded_prefix(self):
        text = "甲" * 45 + "。" + "乙" * 45 + "。"
        plugin = _make_plugin(self, lambda _event: AgentResponse(text, "close-trace"), segmented_send_interval_seconds=0)
        event = FakeEvent("incoming")
        stream = plugin.on_private_message(event)
        first = await anext(stream)
        await stream.aclose()
        history = plugin._session_manager.history(event.unified_msg_origin)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].assistant_text, _plain_text(first))
        self.assertEqual(history[0].assistant_text, "甲" * 45 + "。")

    async def test_qq_official_sends_voice_as_own_message_before_whole_sentences(self) -> None:
        timeline: list[tuple[str, object]] = []
        event = QQOfficialEvent("incoming", timeline)
        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse("第一句。第二句。", "trace-qq"),
            media=RenderedMedia(audio_path="fake-audio.wav"),
            segmented_send_interval_seconds=0,
        )
        yielded = [result async for result in plugin.on_private_message(event)]
        self.assertEqual(len(yielded), 3)
        self.assertEqual(
            [item.__class__.__name__ for item in yielded[0][1]], ["Record"]
        )
        self.assertEqual([_plain_text(result) for result in yielded[1:]], ["第一句。", "第二句。"])

    async def test_unsupported_qq_guild_voice_falls_back_to_text_and_records_text(self) -> None:
        event = QQOfficialEvent("incoming", [])
        event.message_obj.raw_message = type("Message", (), {})()
        text = "语音适配器不支持这个入口时仍应发送文字。"
        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse(text, "trace-qq-guild"),
            media=RenderedMedia(audio_path="fake-audio.wav"),
            media_send_text_with_audio=False,
        )

        yielded = [result async for result in plugin.on_private_message(event)]

        self.assertEqual([_plain_text(result) for result in yielded], [text])
        self.assertFalse(any(
            component.__class__.__name__ == "Record"
            for kind, payload in yielded if kind == "chain"
            for component in payload
        ))
        history = plugin._session_manager.history("bot-id:FriendMessage:user-1")
        self.assertEqual(history[-1].assistant_text, text)

    async def test_segments_preserve_text_audio_once_and_send_emoji_after_final_text(self) -> None:
        interval = 0.25
        emoji_delay = 0.75
        original_text = "甲" * 60 + "。" + "乙" * 60 + "。" + "丙" * 20 + "。"
        timeline: list[tuple[str, object]] = []
        event = RecordingEvent("incoming", timeline)
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)
            timeline.append(("sleep", seconds))

        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse(original_text, "trace-a"),
            media=RenderedMedia(audio_path="fake-audio.wav", emoji_path="fake-emoji.png"),
            segmented_send_interval_seconds=interval,
            media_emoji_delay=emoji_delay,
        )
        yielded = []

        with patch.object(plugin_main.asyncio, "sleep", new=fake_sleep):
            async for result in plugin.on_private_message(event):
                yielded.append(result)
                timeline.append(("yield", _plain_text(result)))

        segment_texts = [_plain_text(result) for result in yielded]
        self.assertEqual("".join(segment_texts), original_text)
        self.assertEqual(segment_texts, ["甲" * 60 + "。", "乙" * 60 + "。", "丙" * 20 + "。"])
        audio_components = [
            component
            for kind, payload in yielded
            if kind == "chain"
            for component in payload
            if component.__class__.__name__ == "Record"
        ]
        self.assertEqual(len(audio_components), 1)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(delays, [interval, interval, emoji_delay])

        yield_indices = [
            index for index, (kind, _value) in enumerate(timeline) if kind == "yield"
        ]
        interval_indices = [
            index
            for index, (kind, value) in enumerate(timeline)
            if kind == "sleep" and value == interval
        ]
        self.assertEqual(len(interval_indices), len(yield_indices) - 1)
        for previous, sleep_index, following in zip(
            yield_indices, interval_indices, yield_indices[1:]
        ):
            self.assertLess(previous, sleep_index)
            self.assertLess(sleep_index, following)

        last_text_index = max(
            index for index, (kind, _value) in enumerate(timeline) if kind == "yield"
        )
        emoji_send_index = next(
            index for index, (kind, _value) in enumerate(timeline) if kind == "event.send"
        )
        self.assertGreater(emoji_send_index, last_text_index)

    async def test_same_session_handlers_do_not_interleave_segments(self) -> None:
        timeline: list[tuple[str, object]] = []
        first_text = "A" * 50 + "。" + "A" * 50 + "。"
        second_text = "B" * 50 + "。" + "B" * 50 + "。"
        event_a = RecordingEvent("first", timeline)
        event_b = RecordingEvent("second", timeline)
        segment_pause_started = asyncio.Event()
        release_segment_pause = asyncio.Event()
        second_entered_handler = asyncio.Event()
        second_response_started = asyncio.Event()

        async def response_for_event(agent_event) -> AgentResponse:
            if agent_event.text == "second":
                second_response_started.set()
            text = first_text if agent_event.text == "first" else second_text
            return AgentResponse(text, f"trace-{agent_event.text}")

        async def on_user_event(agent_event) -> bool:
            if agent_event.text == "second":
                second_entered_handler.set()
            return True

        async def controlled_sleep(_seconds: float) -> None:
            if not release_segment_pause.is_set():
                segment_pause_started.set()
                await release_segment_pause.wait()

        plugin = _make_plugin(
            self,
            response_for_event,
            segmented_send_interval_seconds=0.5,
        )
        plugin._agent_loop.on_user_event = on_user_event

        async def collect(event: FakeEvent, label: str) -> None:
            async for result in plugin.on_private_message(event):
                timeline.append((label, _plain_text(result)))

        with patch.object(plugin_main.asyncio, "sleep", new=controlled_sleep):
            first_task = asyncio.create_task(collect(event_a, "first"))
            await segment_pause_started.wait()
            second_task = asyncio.create_task(collect(event_b, "second"))

            try:
                # Intake intentionally remains outside the data/send locks so
                # new input can invalidate stale work. Only generation/output
                # must wait until the preceding segmented delivery finishes.
                await asyncio.wait_for(second_entered_handler.wait(), timeout=2)
                self.assertFalse(second_response_started.is_set())
                self.assertEqual(
                    [(label, text) for label, text in timeline if label in {"first", "second"}],
                    [("first", "A" * 50 + "。")],
                )
            finally:
                release_segment_pause.set()
                await asyncio.gather(first_task, second_task)

        chunks = [(label, text) for label, text in timeline if label in {"first", "second"}]
        self.assertEqual(
            chunks,
            [
                ("first", "A" * 50 + "。"),
                ("first", "A" * 50 + "。"),
                ("second", "B" * 50 + "。"),
                ("second", "B" * 50 + "。"),
            ],
        )

    async def test_sleep_failure_after_first_yield_does_not_replay_fallback(self) -> None:
        text = "F" * 50 + "。" + "F" * 40 + "。"
        event = FakeEvent("incoming")

        async def failing_sleep(_seconds: float) -> None:
            raise RuntimeError("segment delay failed")

        plugin = _make_plugin(
            self,
            lambda _event: AgentResponse(text, "trace-failure"),
            segmented_send_interval_seconds=0.5,
        )
        yielded = []

        with patch.object(plugin_main.asyncio, "sleep", new=failing_sleep):
            async for result in plugin.on_private_message(event):
                yielded.append(result)

        self.assertEqual([_plain_text(result) for result in yielded], ["F" * 50 + "。"])
        self.assertTrue(event.stopped)
        self.assertNotIn(("plain", plugin._config.fallback_text), yielded)


if __name__ == "__main__":
    unittest.main()
