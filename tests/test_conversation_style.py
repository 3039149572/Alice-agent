from __future__ import annotations

import json
import unittest

from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.history import ConversationTurn
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.generation.response_generator import ResponseGenerator


class ConversationStyleGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ContextBuilder()
        self.perception = PerceptionResult()
        self.state = AgentState()
        self.event = AgentEvent(
            event_id="style-event",
            trace_id="style-trace",
            type="private",
            source="test",
            session_id="style-session",
            user_id="user",
            timestamp=100.0,
            text="你好",
        )

    def _guidance(
        self,
        event: AgentEvent | None = None,
        history: list[ConversationTurn] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        current_event = event or self.event
        turns = history or []
        _planner_system, planner_prompt = self.context.for_planner(
            current_event, self.perception, self.state, [], turns,
        )
        _response_system, response_prompt = self.context.for_response(
            current_event, self.perception, self.state, [], ActionPlan(), turns,
        )
        planner_payload = json.loads(planner_prompt)
        response_payload = json.loads(response_prompt)
        return (
            planner_payload["conversation_style_guidance"],
            response_payload["conversation_style_guidance"],
        )

    def test_empty_or_suppressed_history_has_no_delivery_pattern(self) -> None:
        suppressed = [
            ConversationTurn("第一个问题", None, 1.0),
            ConversationTurn("第二个问题", None, 2.0),
        ]
        for history in ([], suppressed):
            planner, response = self._guidance(history=history)
            self.assertEqual(planner, response)
            self.assertEqual(planner["recent_delivery_patterns"], [])
            self.assertEqual(planner["current_message_style_signals"], [])

    def test_actual_deliveries_drive_repeated_opening_and_question_hints(self) -> None:
        history = [
            ConversationTurn("前一条", "好的，我先看一下。", 1.0),
            ConversationTurn("未送达", None, 2.0),
            ConversationTurn("后一条", "好的，这里有个细节想确认？", 3.0),
        ]
        planner, response = self._guidance(history=history)
        self.assertEqual(planner, response)
        self.assertEqual(len(planner["recent_delivery_patterns"]), 1)

        history.append(ConversationTurn("被抑制", None, 4.0))
        planner, _response = self._guidance(history=history)
        self.assertIn("重复使用了相同的通用开场", planner["recent_delivery_patterns"][0])
        self.assertEqual(len(planner["recent_delivery_patterns"]), 1)

    def test_question_hint_requires_two_delivered_prose_questions(self) -> None:
        history = [
            ConversationTurn("a", "你想先看哪一种？", 1.0),
            ConversationTurn("suppressed", None, 2.0),
            ConversationTurn("b", "需要我继续吗?", 3.0),
        ]
        planner, _response = self._guidance(history=history)
        self.assertEqual(len(planner["recent_delivery_patterns"]), 1)
        self.assertIn("以问题收尾", planner["recent_delivery_patterns"][0])

        planner, _response = self._guidance(history=history[-1:])
        self.assertEqual(planner["recent_delivery_patterns"], [])

    def test_quoted_questions_and_code_are_not_treated_as_assistant_questions(self) -> None:
        history = [
            ConversationTurn("a", "他当时问：“你现在方便吗？”", 1.0),
            ConversationTurn(
                "b", "```python\nprint('Are you ready?')\n```", 2.0,
            ),
        ]
        planner, _response = self._guidance(history=history)
        self.assertEqual(planner["recent_delivery_patterns"], [])

    def test_latest_merged_length_request_and_topic_correction_are_preserved(self) -> None:
        event = AgentEvent(
            event_id="merged",
            trace_id="trace",
            type="private",
            source="test",
            session_id="session",
            user_id="user",
            timestamp=100.0,
            text="请详细解释连接池的用途\n我不是问连接池，我想问的是线程池，请简短点",
            payload={"user_messages": [
                {"text": "请详细解释连接池的用途"},
                {"text": "我不是问连接池，我想问的是线程池，请简短点"},
            ]},
        )
        planner, response = self._guidance(event)
        self.assertEqual(planner, response)
        self.assertEqual(planner["recent_delivery_patterns"], [])
        requests = planner["current_message_style_signals"]
        self.assertEqual(len(requests), 2)
        self.assertIn("可能表达希望压缩回答", requests[0])
        self.assertIn("复杂或技术问题仍须保留必要解释", requests[0])
        self.assertIn("最新明确的纠正", requests[1])

    def test_short_preference_does_not_force_truncation_or_remove_code_formatting(self) -> None:
        event = AgentEvent(
            event_id="complex",
            trace_id="trace",
            type="private",
            source="test",
            session_id="session",
            user_id="user",
            timestamp=100.0,
            text="请简短说明如何修复这个并发问题",
        )
        response_system, response_prompt = self.context.for_response(
            event, self.perception, self.state, [], ActionPlan(), [],
        )
        guidance = json.loads(response_prompt)["conversation_style_guidance"]
        self.assertIn("复杂或技术问题仍须保留必要解释、步骤、代码和约束", guidance["current_message_style_signals"][0])
        self.assertIn("复杂问题必需的原因、步骤、代码或约束", response_system)
        self.assertIn("用户要求 JSON、其他结构化内容或代码时，按要求给出对应格式", response_system)
        self.assertIn("可以使用必要的 Markdown 和代码块", response_system)
        self.assertIn("遵守用户明确指定的格式", response_system)

    def test_narration_and_quoted_style_examples_are_only_heuristic_signals(self) -> None:
        event = AgentEvent(
            event_id="quoted",
            trace_id="trace",
            type="private",
            source="test",
            session_id="session",
            user_id="user",
            timestamp=100.0,
            text='解释器报错了；我不想听解释；我并不想听详细解释；不要简单说。例子是“详细解释”。他让我详细解释一下。',
        )
        planner, response = self._guidance(event)
        self.assertEqual(planner, response)
        signals = planner["current_message_style_signals"]
        self.assertTrue(signals)
        self.assertIn("可能表达", signals[0])
        response_system, _prompt = self.context.for_response(
            event, self.perception, self.state, [], ActionPlan(), [],
        )
        self.assertIn("可能把叙述误判为偏好", response_system)
        self.assertIn("对照 current_messages 和 current_message 的原话", response_system)

    def test_style_hints_do_not_leak_between_context_builds(self) -> None:
        detailed = AgentEvent(
            event_id="detail",
            trace_id="trace",
            type="private",
            source="test",
            session_id="session-a",
            user_id="user-a",
            timestamp=100.0,
            text="请详细解释一下",
        )
        plain = AgentEvent(
            event_id="plain",
            trace_id="trace-2",
            type="private",
            source="test",
            session_id="session-b",
            user_id="user-b",
            timestamp=101.0,
            text="你好",
        )
        self.assertTrue(self._guidance(detailed)[0]["current_message_style_signals"])
        self.assertEqual(self._guidance(plain)[0]["current_message_style_signals"], [])


class ResponseFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_and_json_responses_pass_through_without_rewriting(self) -> None:
        outputs = (
            "```python\nprint('hello')\n```",
            '{"ok": true, "count": 2}',
        )
        for expected in outputs:
            llm = _RecordingLLM(expected)
            generator = ResponseGenerator(llm, ContextBuilder())
            response = await generator.generate(
                AgentEvent(
                    event_id="format",
                    trace_id="trace",
                    type="private",
                    source="test",
                    session_id="session",
                    user_id="user",
                    timestamp=100.0,
                    text="按原样给我代码或 JSON",
                ),
                PerceptionResult(), AgentState(), [], ActionPlan(), [],
            )
            self.assertEqual(response.text, expected)
            self.assertEqual(len(llm.requests), 1)
            self.assertEqual(llm.requests[0].stage, "response")


class _RecordingLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return LLMResult(self.text)


if __name__ == "__main__":
    unittest.main()
