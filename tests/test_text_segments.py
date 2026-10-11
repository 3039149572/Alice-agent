from __future__ import annotations

import asyncio
import unittest
from typing import Any

from astrbot_plugin_alice_agent.adapters.astrbot.proactive_sender import (
    AstrBotProactiveSender,
)
from astrbot_plugin_alice_agent.adapters.astrbot.text_segments import split_text


class CapturingLogger:
    def __init__(self) -> None:
        self.exceptions: list[str] = []

    def exception(self, message: str) -> None:
        self.exceptions.append(message)


class FakeContext:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.messages: list[tuple[str, str]] = []
        self.order: list[str] = []

    async def send_message(self, session_id: str, chain: Any) -> Any:
        text = "".join(getattr(item, "text", str(item)) for item in chain.chain)
        self.messages.append((session_id, text))
        self.order.append(f"send:{text}")
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None


class TextSegmentTests(unittest.TestCase):
    def test_round_trip_for_empty_unicode_and_mixed_newlines(self) -> None:
        samples = [
            "",
            "中文🙂 English\r\nsecond paragraph\n\nlast line  ",
            "  leading and trailing spaces  ",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual("".join(split_text(text, 7)), text)

    def test_prefers_paragraph_and_sentence_boundaries(self) -> None:
        paragraphs = split_text("第一段内容。\n\n第二段内容。", 20)
        self.assertEqual(paragraphs, ["第一段内容。\n\n", "第二段内容。"])

        sentences = split_text("甲句。乙句。", 3)
        self.assertEqual(sentences, ["甲句。", "乙句。"])

    def test_fenced_code_is_kept_intact_even_when_over_limit(self) -> None:
        text = "说明。\n\n```python\n" + ("print('x')\n" * 8) + "```\n\n结尾。"
        chunks = split_text(text, 12)

        self.assertEqual("".join(chunks), text)
        code_chunks = [chunk for chunk in chunks if "```" in chunk]
        self.assertEqual(len(code_chunks), 1)
        self.assertTrue(code_chunks[0].startswith("```python\n"))
        self.assertTrue(code_chunks[0].rstrip("\r\n").endswith("```"))
        self.assertGreater(len(code_chunks[0]), 12)

    def test_long_unpunctuated_text_remains_a_whole_sentence(self) -> None:
        words = split_text("alpha beta gamma delta", 9)
        self.assertEqual(words, ["alpha beta gamma delta"])

        token = split_text("x" * 23, 5)
        self.assertEqual(token, ["x" * 23])

    def test_splits_each_whole_sentence_even_below_soft_limit(self) -> None:
        self.assertEqual(split_text("甲句。乙句。", 100), ["甲句。", "乙句。"])
        self.assertEqual(split_text("价格是3.14元。下一句。", 4), ["价格是3.14元。", "下一句。"])

    def test_trailing_whitespace_stays_with_the_final_text_segment(self) -> None:
        for text, limit in (("12345    ", 5), ("A。    ", 2)):
            with self.subTest(text=text):
                chunks = split_text(text, limit)
                self.assertEqual("".join(chunks), text)
                self.assertFalse(any(not chunk.strip() for chunk in chunks))

    def test_rejects_nonpositive_limit(self) -> None:
        with self.assertRaises(ValueError):
            split_text("text", 0)


class ProactiveSenderSegmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_segments_in_order_with_delay_only_between_them(self) -> None:
        context = FakeContext()
        logger = CapturingLogger()

        async def fake_sleep(seconds: float) -> None:
            context.order.append(f"sleep:{seconds}")

        sender = AstrBotProactiveSender(
            context,
            logger,
            split_enabled=True,
            split_max_chars=3,
            split_interval_seconds=0.25,
            sleep_func=fake_sleep,
        )

        self.assertTrue(await sender.send("session-1", "甲句。乙句。"))
        self.assertEqual(
            context.messages,
            [("session-1", "甲句。"), ("session-1", "乙句。")],
        )
        self.assertEqual(
            context.order,
            ["send:甲句。", "sleep:0.25", "send:乙句。"],
        )
        self.assertEqual(logger.exceptions, [])

    async def test_same_session_lock_covers_the_whole_segment_sequence(self) -> None:
        context = FakeContext()
        logger = CapturingLogger()
        locks: dict[str, asyncio.Lock] = {}
        sleep_started = asyncio.Event()
        release_sleep = asyncio.Event()

        def session_lock(session_id: str) -> asyncio.Lock:
            return locks.setdefault(session_id, asyncio.Lock())

        async def fake_sleep(_seconds: float) -> None:
            sleep_started.set()
            await release_sleep.wait()

        sender = AstrBotProactiveSender(
            context,
            logger,
            split_enabled=True,
            split_max_chars=3,
            split_interval_seconds=0.5,
            session_lock=session_lock,
            sleep_func=fake_sleep,
        )
        proactive = asyncio.create_task(sender.send("same-session", "甲句。乙句。"))
        await sleep_started.wait()

        async def send_reactive() -> None:
            async with session_lock("same-session"):
                context.order.append("send:reactive")
                context.messages.append(("same-session", "reactive"))

        reactive = asyncio.create_task(send_reactive())
        await asyncio.sleep(0)
        self.assertNotIn(("same-session", "reactive"), context.messages)
        release_sleep.set()

        self.assertTrue(await proactive)
        await reactive
        self.assertEqual(
            context.messages,
            [
                ("same-session", "甲句。"),
                ("same-session", "乙句。"),
                ("same-session", "reactive"),
            ],
        )

    async def test_false_result_stops_without_replaying_or_sending_later_chunks(self) -> None:
        context = FakeContext(results=[None, False, None])
        logger = CapturingLogger()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        delivered_history: list[str] = []

        async def record_history(text: str) -> None:
            delivered_history.append(text)

        sender = AstrBotProactiveSender(
            context,
            logger,
            split_enabled=True,
            split_max_chars=3,
            split_interval_seconds=0.2,
            sleep_func=fake_sleep,
        )

        outcome = await sender.send(
            "session-2", "甲句。乙句。丙句。", on_delivered=record_history
        )
        self.assertFalse(outcome)
        self.assertTrue(outcome.partial)
        self.assertEqual(outcome.delivered_chunks, 1)
        self.assertEqual(outcome.delivered_text, "甲句。")
        self.assertEqual(delivered_history, ["甲句。"])
        self.assertEqual(
            context.messages,
            [("session-2", "甲句。"), ("session-2", "乙句。")],
        )
        self.assertEqual(sleeps, [0.2])
        self.assertEqual(logger.exceptions, [])

    async def test_revision_guard_runs_after_waiting_for_session_lock(self) -> None:
        context = FakeContext()
        logger = CapturingLogger()
        lock = asyncio.Lock()
        sender = AstrBotProactiveSender(
            context, logger, session_lock=lambda _session: lock
        )
        valid = True
        guard_calls = 0

        async def guard() -> bool:
            nonlocal guard_calls
            guard_calls += 1
            return valid

        async with lock:
            pending = asyncio.create_task(
                sender.send("same-session", "旧主动消息。", before_send=guard)
            )
            await asyncio.sleep(0)
            valid = False

        outcome = await pending
        self.assertFalse(outcome)
        self.assertEqual(guard_calls, 1)
        self.assertEqual(context.messages, [])

    async def test_revision_change_between_segments_stops_stale_remainder(self) -> None:
        context = FakeContext()
        logger = CapturingLogger()
        valid = True
        guard_calls = 0

        async def guard() -> bool:
            nonlocal guard_calls
            guard_calls += 1
            return valid

        async def user_event_arrives(_seconds: float) -> None:
            nonlocal valid
            valid = False

        sender = AstrBotProactiveSender(
            context,
            logger,
            split_enabled=True,
            split_max_chars=3,
            split_interval_seconds=0.1,
            sleep_func=user_event_arrives,
        )
        history: list[str] = []

        async def record_history(text: str) -> None:
            history.append(text)

        outcome = await sender.send(
            "same-session", "甲句。乙句。", before_send=guard,
            on_delivered=record_history,
        )

        self.assertTrue(outcome.partial)
        self.assertEqual(guard_calls, 2)
        self.assertEqual(context.messages, [("same-session", "甲句。")])
        self.assertEqual(outcome.delivered_text, "甲句。")
        self.assertEqual(history, ["甲句。"])

    async def test_exception_stops_without_replaying_or_sending_later_chunks(self) -> None:
        context = FakeContext(results=[RuntimeError("send failed"), None])
        logger = CapturingLogger()
        sender = AstrBotProactiveSender(
            context,
            logger,
            split_enabled=True,
            split_max_chars=3,
            split_interval_seconds=0,
        )

        self.assertFalse(await sender.send("session-3", "甲句。乙句。"))
        self.assertEqual(context.messages, [("session-3", "甲句。")])
        self.assertEqual(len(logger.exceptions), 1)

    async def test_legacy_two_argument_mode_sends_one_untrimmed_message(self) -> None:
        context = FakeContext()
        sender = AstrBotProactiveSender(context, CapturingLogger())

        self.assertTrue(await sender.send("session-4", "  原样发送  "))
        self.assertEqual(context.messages, [("session-4", "  原样发送  ")])


if __name__ == "__main__":
    unittest.main()
