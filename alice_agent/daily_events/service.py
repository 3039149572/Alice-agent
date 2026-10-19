from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from ..decision.models import DecisionMode, DecisionType
from ..decision.schemas import DAILY_EVENT_SCHEMA, daily_event_questions
from ..domain.history import ConversationTurn
from ..domain.llm import LLMRequest
from ..persona.profile import coerce_persona_profile
from ..knowledge.novel import has_childhood_recollection
from ..perception.parser import parse_json_object
from ..ports.logging import LoggerPort, NullLogger


_TEMPLATES: dict[str, tuple[str, str, dict[str, float]]] = {
    "routine": (
        "Alice整理了手边的东西，让今天的安排重新变得清楚。", "今天整理日常安排",
        {"valence": 0.02, "arousal": -0.02, "dominance": 0.02, "energy": -0.01, "stress": -0.02},
    ),
    "curiosity": (
        "Alice读到一个有趣的问题，花了一会儿琢磨。", "今天读到的问题",
        {"valence": 0.04, "arousal": 0.03, "dominance": 0.01, "energy": -0.02},
    ),
    "small_success": (
        "Alice完成了一件拖延已久的小事，心里稍微轻松了些。", "今天完成的小事",
        {"valence": 0.09, "arousal": 0.03, "dominance": 0.07, "energy": -0.03, "stress": -0.04},
    ),
    "small_setback": (
        "Alice原本的安排被一点小插曲打乱，只好重新调整。", "今天的小插曲",
        {"valence": -0.07, "arousal": 0.03, "dominance": -0.03, "energy": -0.03, "stress": 0.06},
    ),
    "reflection": (
        "Alice安静地回想了一件旧事，又把注意力放回今天。", "今天想到的旧事",
        {"valence": 0.01, "arousal": -0.03, "energy": -0.01},
    ),
    "social": (
        "Alice与熟人有过一次简短交谈，觉得这一天多了点变化。", "今天的一次交谈",
        {"valence": 0.03, "arousal": 0.03, "loneliness": -0.04},
    ),
    "user_related": (
        "Alice想起之前与你说过的一件事，但没有擅自猜测后来如何。", "之前聊过的事",
        {"valence": 0.01, "arousal": 0.02, "loneliness": -0.02},
    ),
    "continuation": (
        "Alice又花了一点时间处理昨天没做完的小事。", "昨天未完的小事",
        {"valence": 0.03, "dominance": 0.02, "energy": -0.02},
    ),
    "rare": (
        "Alice偶然翻到一份旧笔记，停下来认真读了几页。", "偶然翻到的旧笔记",
        {"valence": 0.06, "arousal": 0.05, "dominance": 0.02},
    ),
}
_WEIGHTS = (
    ("routine", 30), ("curiosity", 18), ("small_success", 12),
    ("small_setback", 12), ("reflection", 12), ("social", 12), ("rare", 4),
)
_AXES = ("valence", "arousal", "dominance", "energy", "stress", "loneliness")
_CONTINUABLE_CATEGORIES = frozenset({
    "routine", "curiosity", "small_setback", "reflection", "rare", "continuation",
})
_UNSAFE_STORY_WORDS = ("死亡", "杀人", "自杀", "重伤", "毁灭世界", "世界末日")
_CATEGORY_ANCHORS = {
    "routine": ("整理", "安排"),
    "curiosity": ("读", "问题", "琢磨"),
    "small_success": ("完成", "做好", "解决"),
    "small_setback": ("打乱", "不顺", "耽搁", "调整"),
    "reflection": ("想起", "回想", "回忆"),
    "social": ("交谈", "聊天", "说话"),
    "user_related": ("想起", "记起", "在意"),
    "continuation": ("继续", "昨天", "接着"),
    "rare": ("发现", "翻到", "偶然"),
}


class DailyEventService:
    """One bounded, restart-safe experience batch per local day and session."""

    _NAMESPACE = "alice_daily_events_v1"

    def __init__(
        self,
        storage: object,
        state_manager: object,
        llm: object,
        context_builder: object,
        decisions: object,
        *,
        enabled: bool = False,
        max_events: int = 2,
        llm_enabled: bool = True,
        jev_enabled: bool = True,
        logger: LoggerPort | None = None,
    ) -> None:
        self._storage = storage
        self._states = state_manager
        self._llm = llm
        self._contexts = context_builder
        self._profile = coerce_persona_profile(
            getattr(context_builder, "profile", None)
        )
        self._decisions = decisions
        self._enabled = bool(enabled)
        self._max_events = min(3, max(0, int(max_events)))
        self._llm_enabled = bool(llm_enabled)
        self._jev_enabled = bool(jev_enabled)
        self._logger = logger or NullLogger()

    @staticmethod
    def _rng(session_id: str, day: str) -> random.Random:
        digest = hashlib.sha256(f"{session_id}\0{day}".encode("utf-8")).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    @staticmethod
    def _count(rng: random.Random, maximum: int) -> int:
        roll = rng.random()
        desired = 0 if roll < 0.15 else 1 if roll < 0.65 else 2 if roll < 0.93 else 3
        return min(desired, maximum)

    @staticmethod
    def _can_continue(category: str, stage: int) -> bool:
        return category in _CONTINUABLE_CATEGORIES and stage < 3

    @staticmethod
    def _prior_user_text(history: list[ConversationTurn], day: str) -> str | None:
        current = date.fromisoformat(day)
        for turn in reversed(history):
            try:
                age = (current - datetime.fromtimestamp(turn.timestamp).date()).days
            except (OverflowError, OSError, ValueError):
                continue
            if 0 < age <= 3 and turn.user_text.strip():
                return turn.user_text.strip()[:160]
        return None

    def _seed(
        self, rng: random.Random, index: int, day: str,
        prior_events: list[dict[str, Any]], prior_user_text: str | None,
        used_categories: set[str] | None = None,
    ) -> dict[str, Any]:
        previous = next(
            (
                item for item in reversed(prior_events)
                if item.get("can_continue")
                and self._can_continue(
                    str(item.get("category") or ""), int(item.get("stage", 1))
                )
            ),
            None,
        )
        if index == 0 and previous and rng.random() < 0.25:
            category = "continuation"
            origin = "continuation"
        elif index == 0 and prior_user_text and rng.random() < 0.12:
            category = "user_related"
            origin = "recent_conversation"
        else:
            choices = [
                item for item in _WEIGHTS
                if used_categories is None or item[0] not in used_categories
            ] or list(_WEIGHTS)
            category = rng.choices(
                [item[0] for item in choices],
                weights=[item[1] for item in choices],
            )[0]
            # These are fictional daily-life seeds, not real calendar or social signals.
            origin = "routine_seed" if category == "routine" else "random"
        if used_categories is not None:
            used_categories.add(category)
        fallback, topic = self._profile.event_seed(category, rng)
        if category == "continuation" and previous:
            previous_category = str(previous.get("category") or "")
            if previous_category == "small_setback":
                fallback = (
                    f"{self._profile.name}接着调整了昨天被小插曲打乱的安排，"
                    "今天的节奏顺了一些。"
                )
            elif previous_category == "routine":
                fallback = (
                    f"{self._profile.name}沿着昨天整理日常安排的思路，"
                    "又归整了一件手边小事。"
                )
            elif previous_category in {"curiosity", "reflection", "rare", "continuation"}:
                previous_topic = str(previous.get("topic_hook") or "这条线索")[:20]
                previous_topic = previous_topic.removeprefix("今天").removeprefix("昨天")
                fallback = (
                    f"{self._profile.name}接着琢磨昨天关于{previous_topic}的线索，"
                    "又想到一个小角度。"
                )
        return {
            "category": category,
            "origin": origin,
            "day": day,
            "weekday": date.fromisoformat(day).weekday(),
            "event_seed": fallback,
            "topic_hook": previous.get("topic_hook", topic) if category == "continuation" else topic,
            "previous_event": previous.get("text", "")[:100] if category == "continuation" else "",
            "story_chain": (
                str(previous.get("story_chain")) if category == "continuation"
                else f"{day}-{index}"
            ),
            "stage": int(previous.get("stage", 1)) + 1 if category == "continuation" else 1,
        }

    @staticmethod
    def _safe_text(raw: str, fallback: str, category: str) -> str:
        text = raw.strip().strip('"“”\'')
        if (
            len(text) < 4 or len(text) > 80 or "\n" in text
            or any(word in text for word in _UNSAFE_STORY_WORDS)
            or text.startswith(("{", "[", "#", "-", "```"))
            or not any(anchor in text for anchor in _CATEGORY_ANCHORS[category])
            or has_childhood_recollection(text)
        ):
            return fallback
        return text

    async def _expand(self, session_id: str, seed: dict[str, Any], prior_user_text: str | None) -> str:
        fallback = str(seed["event_seed"])
        if not self._llm_enabled:
            return fallback
        try:
            system_prompt, prompt = self._contexts.for_daily_event(
                seed, prior_user_text if seed["category"] == "user_related" else None
            )
            result = await self._llm.generate(LLMRequest(
                stage="daily_event", session_id=session_id,
                system_prompt=system_prompt, prompt=prompt,
            ))
            return self._safe_text(result.text, fallback, str(seed["category"]))
        except Exception as exc:
            self._logger.warning(f"Alice daily event LLM fallback ({type(exc).__name__})")
            return fallback

    async def _reaction(
        self, seed: dict[str, Any], text: str, state: object, trace_id: str,
        *, session_id: str = "",
    ) -> tuple[dict[str, float], float, str]:
        baseline = {axis: float(_TEMPLATES[str(seed["category"])][2].get(axis, 0.0)) for axis in _AXES}
        if not self._jev_enabled or not getattr(self._decisions, "enabled", False):
            return baseline, 0.35, "bounded_rule"
        try:
            result = await self._decisions.decide(
                {
                    "event": text,
                    "category": seed["category"],
                    "current_mood": {
                        key: value for key, value in asdict(state).items()
                        if key in {"mood_valence", "mood_arousal", "mood_dominance", "energy", "stress", "loneliness"}
                    },
                },
                daily_event_questions(),
                decision_type=DecisionType.DAILY_EVENT.value,
                schema_version=DAILY_EVENT_SCHEMA,
                trace_id=trace_id,
            )
        except Exception as exc:
            self._logger.warning(f"Alice daily event Jev fallback ({type(exc).__name__})")
            result = None
        if self._decisions.mode == DecisionMode.SHADOW:
            return baseline, 0.35, "bounded_rule_shadow_jev"
        if result is None:
            llm_score = await self._llm_reaction(session_id, seed, text, state)
            if llm_score is not None:
                self._decisions.record_escalation(DecisionType.DAILY_EVENT.value)
                return llm_score[0], llm_score[1], "llm_fallback"
            return baseline, 0.35, "bounded_rule"
        if any(axis not in result.answers or result.answers[axis].score is None for axis in (*_AXES, "importance")):
            return baseline, 0.35, "bounded_rule_partial_jev"
        try:
            scores = {axis: float(result.answers[axis].score) for axis in (*_AXES, "importance")}
            if any(not math.isfinite(score) or not 0.0 <= score <= 4.0 for score in scores.values()):
                return baseline, 0.35, "bounded_rule_invalid_jev"
            mapped = {
                axis: max(-0.12, min(0.12, (scores[axis] - 2.0) * 0.06))
                for axis in _AXES
            }
            importance = scores["importance"] / 4.0
        except (TypeError, ValueError, OverflowError):
            return baseline, 0.35, "bounded_rule_invalid_jev"
        low_numeric_keys = {
            axis for axis in (*_AXES, "importance")
            if min(result.confidence, result.answers[axis].effective_confidence)
            < self._decisions.policy.minimum_decision_confidence
        }
        if low_numeric_keys:
            llm_score = await self._llm_reaction(session_id, seed, text, state)
            if llm_score is None:
                return baseline, 0.35, "bounded_rule_low_confidence_jev"
            self._decisions.record_escalation(DecisionType.DAILY_EVENT.value)
            llm_reaction, llm_importance = llm_score
            if result.confidence <= 0.0:
                return llm_reaction, llm_importance, "llm_fallback"
            blended: dict[str, float] = {}
            for axis in _AXES:
                if axis not in low_numeric_keys:
                    blended[axis] = mapped[axis]
                    continue
                confidence = min(result.confidence, result.answers[axis].effective_confidence)
                weight = 0.5 * confidence / self._decisions.policy.minimum_decision_confidence
                blended[axis] = max(-0.12, min(0.12,
                    (1.0 - weight) * llm_reaction[axis] + weight * mapped[axis]
                ))
            if "importance" not in low_numeric_keys:
                return blended, importance, "jev_llm_blend"
            confidence = min(result.confidence, result.answers["importance"].effective_confidence)
            weight = 0.5 * confidence / self._decisions.policy.minimum_decision_confidence
            return blended, (
                (1.0 - weight) * llm_importance + weight * importance
            ), "jev_llm_blend"
        if not self._decisions.policy.may_use_model_decision(self._decisions.mode, result.confidence):
            return baseline, 0.35, "bounded_rule"
        return mapped, importance, "jev_bounded"

    async def _llm_reaction(
        self, session_id: str, seed: dict[str, Any], text: str, state: object,
    ) -> tuple[dict[str, float], float] | None:
        if not self._llm_enabled:
            return None
        try:
            system_prompt, prompt = self._contexts.for_daily_event_reaction(seed, text, state)
            result = await self._llm.generate(LLMRequest(
                stage="daily_event_reaction", session_id=session_id,
                system_prompt=system_prompt, prompt=prompt,
            ))
            data = parse_json_object(result.text)
            if data is None:
                return None
            values = {key: float(data[key]) for key in (*_AXES, "importance")}
            if any(
                isinstance(data[key], bool) or not math.isfinite(value)
                or not (-0.12 <= value <= 0.12 if key in _AXES else 0.0 <= value <= 1.0)
                for key, value in values.items()
            ):
                return None
            return {axis: values[axis] for axis in _AXES}, values["importance"]
        except Exception as exc:
            self._logger.warning(f"Alice daily event LLM score fallback ({type(exc).__name__})")
            return None

    async def ensure(
        self, session_id: str, user_id: str, timestamp: float,
        history: list[ConversationTurn] | None = None,
    ) -> list[dict[str, Any]]:
        if not self._enabled:
            return []
        day = datetime.fromtimestamp(timestamp).date().isoformat()
        current_state = await self._states.get(session_id)
        if current_state.daily_event_day and current_state.daily_event_day > day:
            return []
        key = f"{session_id}\0{day}"
        record = await self._storage.get(self._NAMESPACE, key)
        if isinstance(record, dict) and isinstance(record.get("events"), list):
            events = record["events"]
        else:
            rng = self._rng(session_id, day)
            prior_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            prior = await self._storage.get(self._NAMESPACE, f"{session_id}\0{prior_day}")
            prior_events = prior.get("events", []) if isinstance(prior, dict) else []
            prior_user_text = self._prior_user_text(history or [], day)
            events = []
            used_categories: set[str] = set()
            for index in range(self._count(rng, self._max_events)):
                seed = self._seed(
                    rng, index, day, prior_events, prior_user_text,
                    used_categories=used_categories,
                )
                text = await self._expand(session_id, seed, prior_user_text)
                state = await self._states.get(session_id)
                event_id = hashlib.sha256(f"{key}\0{index}".encode("utf-8")).hexdigest()[:16]
                reaction, importance, reaction_source = await self._reaction(
                    seed, text, state, f"daily_{event_id}", session_id=session_id
                )
                events.append({
                    "event_id": event_id,
                    "day": day,
                    "category": seed["category"],
                    "origin": seed["origin"],
                    "text": text,
                    "topic_hook": seed["topic_hook"],
                    "reaction": reaction,
                    "reaction_source": reaction_source,
                    "importance": importance,
                    "memory_candidate": importance >= 0.75,
                    "story_chain": seed["story_chain"],
                    "stage": seed["stage"],
                    "can_continue": self._can_continue(
                        str(seed["category"]), int(seed["stage"])
                    ),
                    "generated_from": {
                        "type": seed["origin"], "memory_ids": [],
                    },
                })
            # Save the event batch before the mood update. If interrupted here,
            # the next call reuses the same batch and applies it exactly once.
            await self._storage.set(self._NAMESPACE, key, {"day": day, "events": events})
        await self._states.apply_daily(session_id, day, events)
        if events and current_state.daily_event_day != day:
            self._logger.info(f"Alice daily events applied day={day} session={session_id} count={len(events)}")
        return events
