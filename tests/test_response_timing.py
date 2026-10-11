from __future__ import annotations

import asyncio
import unittest

from astrbot_plugin_alice_agent.adapters.astrbot.pending_messages import (
    PendingMessage,
    PendingReplyBatch,
    PendingReplyCoordinator,
)
from astrbot_plugin_alice_agent.alice_agent.decision.models import (
    DecisionAnswer,
    DecisionMode,
    DecisionResult,
)
from astrbot_plugin_alice_agent.alice_agent.decision.policies import DecisionEscalationPolicy
from astrbot_plugin_alice_agent.alice_agent.decision.response_timing import (
    ReplyTimingDecision,
    ResponseTimingService,
)
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent


class FixedTimingModel:
    def __init__(self, action: str, confidence: float = 0.98) -> None:
        self.action = action
        self.confidence = confidence
        self.calls = 0

    async def decide(self, _state, questions, **kwargs):
        self.calls += 1
        options = list(questions["action"].criteria)
        remaining = (1.0 - self.confidence) / max(1, len(options) - 1)
        probabilities = {
            option: self.confidence if option == self.action else remaining
            for option in options
        }
        return DecisionResult(
            "timing-test",
            kwargs["trace_id"],
            kwargs["decision_type"],
            kwargs["schema_version"],
            "",
            "test-model",
            1.0,
            answers={"action": DecisionAnswer(
                "choice",
                choice=self.action,
                probabilities=probabilities,
                confidence=self.confidence,
            )},
        )

    async def close(self) -> None:
        return None


def make_event(text: str, event_id: str = "event") -> AgentEvent:
    return AgentEvent(
        event_id, f"trace-{event_id}", "private", "test", "session", "user",
        1.0, text,
    )


def make_timing(
    action: str,
    *,
    source: str = "local_rule",
    rule_action: str = "wait",
    explicit_wait: bool = False,
    protected_question: bool = False,
) -> ReplyTimingDecision:
    diagnostic = {
        "rule_action": rule_action,
        "explicit_wait": explicit_wait,
        "protected_question": protected_question,
    }
    return ReplyTimingDecision(action, source, 0.98, diagnostic)


def make_pending(
    event_id: str,
    *,
    merge_safe: bool = True,
    timing: ReplyTimingDecision | None = None,
) -> PendingMessage:
    return PendingMessage(
        agent_event=make_event(event_id, event_id),
        framework_event=object(),
        merge_safe=merge_safe,
        revision=1,
        timing=timing,
    )


class ResponseTimingDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def _service(self, mode: DecisionMode, action: str, confidence: float = 0.98):
        model = FixedTimingModel(action, confidence)
        service = ResponseTimingService(
            DecisionService(
                model,
                mode=mode,
                policy=DecisionEscalationPolicy(0.85, 0.60),
                cache_ttl_seconds=0,
            )
        )
        return service, model

    async def test_primary_and_hybrid_can_change_ordinary_message_but_shadow_cannot(self) -> None:
        event = make_event("我今天看了一本书。")
        for mode in (DecisionMode.PRIMARY, DecisionMode.HYBRID):
            service, model = await self._service(mode, "reply_now")
            decision = await service.decide(event)
            self.assertEqual(decision.action, "reply_now")
            self.assertEqual(decision.source, "jev")
            self.assertEqual(model.calls, 1)

        shadow, model = await self._service(DecisionMode.SHADOW, "reply_now")
        decision = await shadow.decide(event)
        self.assertEqual(decision.action, "wait")
        self.assertEqual(decision.source, "local_rule")
        self.assertEqual(model.calls, 1)

    async def test_below_eighty_falls_back_and_primary_accepts_eighty(self) -> None:
        event = make_event("我今天看了一本书。")
        hybrid, _ = await self._service(DecisionMode.HYBRID, "reply_now", 0.70)
        primary, _ = await self._service(DecisionMode.PRIMARY, "reply_now", 0.70)
        self.assertEqual((await hybrid.decide(event)).action, "wait")
        self.assertEqual((await primary.decide(event)).action, "wait")
        accepted, _ = await self._service(DecisionMode.PRIMARY, "reply_now", 0.80)
        selected = await accepted.decide(event)
        self.assertEqual(selected.action, "reply_now")
        self.assertEqual(selected.source, "jev")

    async def test_questions_are_protected_from_model_silence(self) -> None:
        service, _ = await self._service(DecisionMode.PRIMARY, "ignore")
        decision = await service.decide(make_event("这个怎么处理？"))
        self.assertEqual(decision.action, "reply_now")
        self.assertEqual(decision.source, "local_rule")
        self.assertTrue(decision.diagnostic["protected_question"])

    async def test_explicit_no_reply_wins_but_quoted_or_negated_words_do_not(self) -> None:
        explicit = ResponseTimingService.local_fallback(make_event("别回复我这条消息。"))
        self.assertEqual(explicit.action, "ignore")
        self.assertTrue(explicit.batch_boundary)

        quoted = ResponseTimingService.local_fallback(
            make_event("他说“不要回复我”只是个玩笑。")
        )
        self.assertNotEqual(quoted.action, "ignore")
        self.assertFalse(quoted.batch_boundary)

        negated = ResponseTimingService.local_fallback(
            make_event("我不是让你‘别回复我’。")
        )
        self.assertNotEqual(negated.action, "ignore")
        self.assertFalse(negated.batch_boundary)

    async def test_quoted_or_negated_wait_phrase_is_not_an_explicit_wait(self) -> None:
        quoted = ResponseTimingService.local_fallback(
            make_event("他说“我还没说完”只是转述。")
        )
        self.assertFalse(quoted.diagnostic["explicit_wait"])

        negated = ResponseTimingService.local_fallback(
            make_event("不用等我说完了，现在可以回答。")
        )
        self.assertFalse(negated.diagnostic["explicit_wait"])

    async def test_explicit_wait_has_priority_until_a_later_protected_question(self) -> None:
        waiting = PendingReplyBatch(
            "session",
            [make_pending(
                "wait", timing=make_timing(
                    "wait", explicit_wait=True,
                ),
            )],
        )
        self.assertEqual(waiting.action(), "wait")

        completed_with_question = PendingReplyBatch(
            "session",
            [
                make_pending("wait", timing=make_timing("wait", explicit_wait=True)),
                make_pending("question", timing=make_timing(
                    "reply_now", rule_action="reply_now", protected_question=True,
                )),
            ],
        )
        self.assertEqual(completed_with_question.action(), "reply_now")


class PendingReplyCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_boundary_suppresses_unprocessed_batch_and_separates_following_turn(self) -> None:
        coordinator = PendingReplyCoordinator(max_messages=8)
        first, first_owner = await coordinator.submit(
            make_pending("before-no-reply", timing=make_timing("wait"))
        )
        first.phase = "waiting"
        self.assertTrue(first_owner)

        no_reply_timing = ResponseTimingService.local_fallback(
            make_event("别回复我这条消息。", "no-reply")
        )
        stopped, no_reply_owner = await coordinator.submit(
            make_pending("no-reply", timing=no_reply_timing),
            boundary=True,
            cancel_current=True,
        )
        self.assertIs(stopped, first)
        self.assertFalse(no_reply_owner)
        self.assertEqual(stopped.action(), "ignore")
        self.assertTrue(stopped.boundary)

        after, after_owner = await coordinator.submit(
            make_pending("after-no-reply", timing=make_timing("reply_now"))
        )
        self.assertTrue(after_owner)
        self.assertIsNot(after, stopped)
        self.assertEqual(
            [item.agent_event.event_id for item in stopped.items],
            ["before-no-reply", "no-reply"],
        )

        await coordinator.finish(stopped)
        await coordinator.finish(after)

    async def test_media_item_starts_a_new_batch_and_cannot_be_joined_retroactively(self) -> None:
        coordinator = PendingReplyCoordinator(max_messages=8)
        first, first_owner = await coordinator.submit(make_pending("text-1"))
        self.assertTrue(first_owner)

        media, media_owner = await coordinator.submit(
            make_pending("media", merge_safe=False)
        )
        self.assertTrue(media_owner)
        self.assertIsNot(media, first)

        later_text, text_owner = await coordinator.submit(make_pending("text-2"))
        self.assertTrue(text_owner)
        self.assertIsNot(later_text, media)
        self.assertEqual([item.agent_event.event_id for item in first.items], ["text-1"])
        self.assertEqual([item.agent_event.event_id for item in media.items], ["media"])

        await coordinator.finish(first)
        await coordinator.finish(media)
        await coordinator.finish(later_text)

    async def test_batches_are_fifo_and_cancel_wakes_a_waiting_turn(self) -> None:
        coordinator = PendingReplyCoordinator()
        first, _ = await coordinator.submit(make_pending("first"))
        second, _ = await coordinator.submit(
            make_pending("second"), boundary=True,
        )
        waiting = asyncio.create_task(coordinator.wait_turn(second))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())

        await coordinator.finish(first)
        self.assertTrue(await asyncio.wait_for(waiting, timeout=0.2))
        await coordinator.finish(second)

        coordinator = PendingReplyCoordinator()
        first, _ = await coordinator.submit(make_pending("first"))
        second, _ = await coordinator.submit(
            make_pending("second"), boundary=True,
        )
        waiting = asyncio.create_task(coordinator.wait_turn(second))
        await asyncio.sleep(0)
        await coordinator.cancel_all()
        self.assertFalse(await asyncio.wait_for(waiting, timeout=0.2))
        await coordinator.finish(first)
        await coordinator.finish(second)


if __name__ == "__main__":
    unittest.main()
