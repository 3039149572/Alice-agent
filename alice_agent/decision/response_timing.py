from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .models import DecisionMode, DecisionType
from .schemas import RESPONSE_TIMING_SCHEMA, response_timing_questions


_NO_REPLY = re.compile(
    r"^(?:alice[，,:： ]*)?(?:不用|不必|不需要|别|不要|请不要)(?:再)?"
    r"(?:回(?:复)?(?:我)?|回答(?:我)?|理我)(?:这条(?:消息)?|这个问题)?"
    r"(?:了|啦|吧|就行|即可|也可以)?[。！!… ]*$",
    re.IGNORECASE,
)
_WAIT = (
    "我还没说完", "我还没讲完", "等我说完", "等我讲完", "让我说完",
    "让我讲完", "我继续说", "我接着说", "我还要说", "先别急着回",
    "稍等我一下", "等一下我继续", "先听我说完",
)
_QUESTION_OR_HELP = re.compile(
    r"[?？]|(?:吗|么|呢)[。！!… ]*$|(?:谁|什么|哪个|哪些|哪种|多少|几岁|哪里|在哪|什么时候|几时|几点|为何|怎么|如何|为什么|为啥|能不能|可以吗|帮我|请帮|求助|怎么办|教我|问(?:一下|个)?(?:问题|你))"
)
_WAIT_QUESTION = re.compile(r"(?:能不能|可以|请|麻烦)?(?:先)?等(?:我)?(?:一下|一会儿)?(?:说完|讲完)(?:再回|再说)?吗?[？?。！!]*$")
_WAIT_NEGATED_OR_MENTIONED = re.compile(
    r"(?:不是|不要|不必|不用|别|他说|她说|意思是|是什么意思|解释|引用).{0,8}(?:等我说完|等我讲完|让我说完|让我讲完)"
)
_WAIT_PHRASE_PATTERN = "|".join(re.escape(phrase) for phrase in _WAIT)
_WAIT_MENTIONED = re.compile(
    r"(?:他说|她说|对方说|原话|转述|引用|提到).{0,24}(?:"
    + _WAIT_PHRASE_PATTERN
    + r")|(?:"
    + _WAIT_PHRASE_PATTERN
    + r").{0,16}(?:只是转述|是转述|是什么意思|怎么理解|指什么)"
)
_CLOSED_TOPIC = re.compile(r"(?:晚安|再见|拜拜|先睡了|我下线了|不聊了)[。！!～~… ]*$")


@dataclass(frozen=True, slots=True)
class ReplyTimingDecision:
    action: str
    source: str
    confidence: float
    diagnostic: dict[str, Any]
    batch_boundary: bool = False


class ResponseTimingService:
    """Bounded timing decision; deterministic user intent always wins safety checks."""

    def __init__(self, decisions: object, *, enabled: bool = True) -> None:
        self._decisions = decisions
        self._enabled = bool(enabled)

    @classmethod
    def has_batch_boundary(cls, text: str) -> bool:
        _action, boundary = cls._rules(text)
        return boundary

    @classmethod
    def is_no_reply_command(cls, text: str) -> bool:
        return _NO_REPLY.fullmatch(" ".join((text or "").strip().split())) is not None

    @staticmethod
    def _is_explicit_wait(text: str) -> bool:
        return (
            any(phrase in text for phrase in _WAIT)
            or _WAIT_QUESTION.search(text) is not None
        ) and _WAIT_NEGATED_OR_MENTIONED.search(text) is None and _WAIT_MENTIONED.search(text) is None

    @classmethod
    def local_fallback(cls, event: object, *, status: str = "deadline_fallback") -> ReplyTimingDecision:
        text = str(getattr(event, "text", None) or "")
        action, boundary = cls._rules(text)
        normalized = " ".join(text.strip().split())
        explicit_wait = action == "wait" and cls._is_explicit_wait(normalized)
        protected_question = action == "reply_now" and _QUESTION_OR_HELP.search(text) is not None
        diagnostic = {
            "rule_action": action,
            "selected_action": action,
            "selected_source": "local_rule",
            "confidence": 1.0 if action in {"ignore", "wait"} else 0.6,
            "mode": "off",
            "status": status,
            "latency_ms": None,
            "jev_action": None,
            "batch_boundary": boundary,
            "protected_question": protected_question,
            "explicit_wait": explicit_wait,
        }
        return ReplyTimingDecision(action, "local_rule_fallback", diagnostic["confidence"], diagnostic, boundary)

    @staticmethod
    def _rules(text: str) -> tuple[str, bool]:
        normalized = " ".join((text or "").strip().split())
        if _NO_REPLY.fullmatch(normalized):
            return "ignore", True
        explicit_wait = ResponseTimingService._is_explicit_wait(normalized)
        if explicit_wait:
            return "wait", False
        if _CLOSED_TOPIC.search(normalized):
            return "reply_now", True
        if _QUESTION_OR_HELP.search(normalized):
            return "reply_now", False
        # A short quiet window on ordinary text combines rapid successive
        # messages; direct questions and help requests still go immediately.
        return "wait", False

    async def decide(
        self,
        event: object,
        history: list[object] | None = None,
        current_messages: list[str] | None = None,
    ) -> ReplyTimingDecision:
        text = str(getattr(event, "text", None) or "")
        rule_action, batch_boundary = self._rules(text)
        normalized = " ".join(text.strip().split())
        explicit_wait = rule_action == "wait" and self._is_explicit_wait(normalized)
        protected_question = (
            rule_action == "reply_now" and _QUESTION_OR_HELP.search(text) is not None
        )
        diagnostic: dict[str, Any] = {
            "rule_action": rule_action,
            "selected_action": rule_action,
            "selected_source": "local_rule",
            "confidence": 1.0 if rule_action != "reply_now" else 0.6,
            "mode": getattr(getattr(self._decisions, "mode", None), "value", "off"),
            "status": "rule",
            "latency_ms": None,
            "decision_id": None,
            "jev_action": None,
            "batch_boundary": batch_boundary,
            "protected_question": protected_question,
            "explicit_wait": explicit_wait,
        }
        if not self._enabled or not getattr(self._decisions, "enabled", False):
            diagnostic["status"] = "disabled" if not self._enabled else "jev_unavailable"
            return ReplyTimingDecision(rule_action, "local_rule", diagnostic["confidence"], diagnostic, batch_boundary)

        recent = []
        for turn in (history or [])[-3:]:
            recent.append({
                "user": str(getattr(turn, "user_text", ""))[:240],
                "assistant": str(getattr(turn, "assistant_text", "") or "")[:240],
            })
        messages = [str(item)[:1200] for item in (current_messages or [text])][-6:]
        attempt: dict[str, Any] = {}
        try:
            result = await self._decisions.decide(
                {
                    "current_messages": messages,
                    "user_intent_baseline": rule_action,
                    "recent_delivered_history": recent,
                },
                response_timing_questions(),
                decision_type=DecisionType.RESPONSE_TIMING.value,
                schema_version=RESPONSE_TIMING_SCHEMA,
                trace_id=str(getattr(event, "trace_id", "")),
                attempt_diagnostics=attempt,
            )
        except Exception as exc:
            result = None
            attempt.update(status="error", error_kind=type(exc).__name__)
        diagnostic.update({
            "status": attempt.get("status", "fallback" if result is None else "ok"),
            "latency_ms": attempt.get("api_latency_ms"),
            "decision_id": attempt.get("decision_id"),
            "error_kind": attempt.get("error_kind"),
        })
        if result is None:
            diagnostic["fallback_reason"] = attempt.get("error_kind", "jev_unavailable")
            return ReplyTimingDecision(rule_action, "local_rule_fallback", diagnostic["confidence"], diagnostic, batch_boundary)

        answer = result.answers.get("action")
        jev_action = answer.choice if answer is not None else None
        diagnostic["jev_action"] = jev_action
        diagnostic["confidence"] = float(result.confidence)
        mode = getattr(self._decisions, "mode", DecisionMode.OFF)
        use_jev = (
            mode != DecisionMode.SHADOW
            and getattr(self._decisions, "policy", None) is not None
            and self._decisions.policy.may_use_model_decision(mode, result.confidence)
        )
        if rule_action == "ignore" or explicit_wait or batch_boundary or protected_question:
            selected, source = rule_action, "local_rule"
        elif use_jev and jev_action in {"reply_now", "wait", "ignore"}:
            selected, source = jev_action, "jev"
        else:
            selected, source = rule_action, "local_rule"
        diagnostic["selected_action"] = selected
        diagnostic["selected_source"] = source
        if mode == DecisionMode.SHADOW and hasattr(self._decisions, "record_comparison"):
            self._decisions.record_comparison(
                DecisionType.RESPONSE_TIMING.value,
                str(getattr(event, "trace_id", "")),
                jev_action,
                rule_action,
            )
        return ReplyTimingDecision(selected, source, float(result.confidence), diagnostic, batch_boundary)
