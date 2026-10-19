from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.memory import MemoryItem
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from ..relationship.models import RelationshipState, relationship_summary
from ..drives.models import DriveState
from ..proactive.models import CandidateAction, UserAvailability
from ..persona.profile import PersonaProfile, coerce_persona_profile
from ..knowledge.novel import MEMORY_BOUNDARY, NovelContextConfig, NovelKnowledge
from ..ports.logging import LoggerPort


_STYLE_HISTORY_SCAN_LIMIT = 16
_STYLE_DELIVERED_LIMIT = 4
_STOCK_OPENINGS = (
    "我理解你",
    "听起来",
    "当然可以",
    "没问题",
    "好的",
    "明白",
    "你说得对",
    "确实",
    "可以的",
)
_QUOTED_SPANS = re.compile(
    r"“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|\"[^\"\n]*\""
)
_CODE_FENCES = re.compile(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_BLOCKQUOTE_LINE = re.compile(r"(?m)^\s*>.*(?:\n|$)")
_DETAIL_REQUESTS = re.compile(
    r"(?:详细|具体|充分)(?:地)?(?:解释|说明|讲解|讲|说|分析|回答|写|推导)"
    r"|展开(?:说|讲|解释|说明|分析)"
    r"|(?:一步一步|逐步|从头)(?:解释|说明|讲解|讲|分析|推导)"
    r"|(?:详细|具体)(?:一点|一些|些)"
    r"|(?:请|麻烦|能否|能不能|可以|帮我|给我)(?:你)?"
    r"(?:详细|具体)?(?:地)?(?:解释|说明|讲解|分析|推导)"
    r"|(?:解释|说明|讲解|分析|推导)"
    r"(?:一下|下|清楚(?:点|一点)?|这个|这件事|原因|过程|原理|步骤|为什么|怎么(?:做|用|实现|算)?)"
    r"|讲明白|说清楚"
)
_CONCISE_REQUESTS = re.compile(
    r"简短(?:点|一点|些|回答(?:一下|些)?|说明(?:一下|些)?)"
    r"|(?:更)?简洁(?:点|一点|一些|些|点儿|些儿)"
    r"|(?:简单|簡單)(?:地)?(?:说|講|回答|解释|說|講解)(?:一下|下)?"
    r"|一句话"
    r"|只要(?:结果|結論|结论|重点|要点)"
    r"|(?:别|不要|不用|不需要)太长"
    r"|(?:不用|不需要|别|不要)展开"
    r"|直接给(?:出)?(?:答案|结论)"
    r"|短一点"
    r"|少说一点"
    r"|精简一点"
    r"|(?:别|不要|不用|不需要)长篇大论"
)
_NEGATED_STYLE_PREFIX = re.compile(
    r"(?:不|没)(?:太|再|继续)?(?:想|要|需要|必|打算|愿意|希望|准备)"
    r"(?:要|听|听到|看|看到|再|继续|去|你)?$"
    r"|(?:别|不要|不用|不必|不需要)(?:再|继续|太|过于|很|听|看|你)?$"
)
_TOPIC_CORRECTION = re.compile(
    r"不是.{0,48}(?:我是说|我指的是|我问的是|我想问的是|而是)"
    r"|(?:不是这个意思|你理解错了|我问的是|我想问的是|我指的是|"
    r"我的意思是|更正一下|纠正一下|准确地?说|重新说一下)"
)


def _visible_prose(text: str) -> str:
    """Remove quoted and code examples before classifying conversational form."""
    visible = _CODE_FENCES.sub(" ", text)
    visible = _INLINE_CODE.sub(" ", visible)
    visible = _BLOCKQUOTE_LINE.sub(" ", visible)
    return _QUOTED_SPANS.sub(" ", visible)


def _ends_with_question(text: str) -> bool:
    visible = _visible_prose(text).rstrip()
    visible = visible.rstrip("*_~)]}）】》 \t\r\n")
    return bool(visible) and visible[-1] in "?？"


def _opening_key(text: str) -> str | None:
    visible = _visible_prose(text).lstrip(" \t\r\n*#>-•")
    for opening in _STOCK_OPENINGS:
        if visible.startswith(opening):
            return opening
    return None


def _recent_delivered_texts(history: list[ConversationTurn]) -> list[str]:
    """Inspect a bounded tail while skipping undelivered/suppressed turns."""
    delivered: list[str] = []
    for turn in reversed(history[-_STYLE_HISTORY_SCAN_LIMIT:]):
        text = turn.assistant_text
        if isinstance(text, str) and text.strip():
            delivered.append(text)
            if len(delivered) >= _STYLE_DELIVERED_LIMIT:
                break
    return list(reversed(delivered))


def _conversation_style_guidance(
    current_messages: list[dict[str, object]], history: list[ConversationTurn]
) -> dict[str, list[str]]:
    """Return small, deterministic hints; never copy message or reply text into prompts."""
    history_patterns: list[str] = []
    delivered = _recent_delivered_texts(history)
    opening_counts: dict[str, int] = {}
    for text in delivered:
        opening = _opening_key(text)
        if opening is not None:
            opening_counts[opening] = opening_counts.get(opening, 0) + 1
    if any(count >= 2 for count in opening_counts.values()):
        history_patterns.append(
            "最近几条实际发出的回复重复使用了相同的通用开场；本轮可以直接进入内容或换一种自然说法。"
        )
    if len(delivered) >= 2 and all(
        _ends_with_question(text) for text in delivered[-2:]
    ):
        history_patterns.append(
            "最近连续两条实际发出的回复都以问题收尾；只有追问确实有帮助时才继续提问。"
        )

    latest_length_request: tuple[int, int, str] | None = None
    topic_corrected = False
    for message_index, message in enumerate(current_messages[-12:]):
        raw_text = message.get("text")
        if not isinstance(raw_text, str):
            continue
        visible = _visible_prose(raw_text)
        if _TOPIC_CORRECTION.search(visible):
            topic_corrected = True
        for kind, pattern in (
            ("detailed", _DETAIL_REQUESTS),
            ("concise", _CONCISE_REQUESTS),
        ):
            for match in pattern.finditer(visible):
                if _NEGATED_STYLE_PREFIX.search(visible[max(0, match.start() - 10):match.start()]):
                    continue
                candidate = (message_index, match.end(), kind)
                if latest_length_request is None or candidate[:2] >= latest_length_request[:2]:
                    latest_length_request = candidate

    current_signals: list[str] = []
    if latest_length_request is not None:
        if latest_length_request[2] == "detailed":
            current_signals.append(
                "当前消息组出现了解释或展开类措辞，可能表达希望讲得更清楚；对照用户原话确认后，再按问题需要说明原因、过程或步骤。"
            )
        else:
            current_signals.append(
                "当前消息组出现了简洁类措辞，可能表达希望压缩回答；对照用户原话确认后再调整，复杂或技术问题仍须保留必要解释、步骤、代码和约束。"
            )
    if topic_corrected:
        current_signals.append(
            "当前消息组出现了话题纠正信号；对照用户原话确认后，以最新明确的纠正为准，不沿用先前误解。"
        )
    return {
        "recent_delivery_patterns": history_patterns,
        "current_message_style_signals": current_signals,
    }


class ContextBuilder:
    def __init__(
        self,
        package_root: Path | None = None,
        profile: PersonaProfile | dict[str, object] | None = None,
        novel_config: NovelContextConfig | None = None,
        logger: LoggerPort | None = None,
    ) -> None:
        self._root = package_root or Path(__file__).resolve().parents[1]
        self._prompt_dir = self._root / "prompts"
        self._persona_dir = self._root / "persona"
        self._cache: dict[Path, str] = {}
        self.profile = coerce_persona_profile(profile)
        config = novel_config or NovelContextConfig()
        directory = Path(config.directory) if config.directory else self._root / "knowledge" / "data"
        self.novel_knowledge = NovelKnowledge(directory, config, logger)

    def _read(self, path: Path) -> str:
        if path not in self._cache:
            self._cache[path] = path.read_text(encoding="utf-8").strip()
        return self._cache[path]

    def _prompt(self, name: str) -> str:
        return self._read(self._prompt_dir / name).replace("Alice", self.profile.name)

    def _novel_context(
        self, text: str, perception: PerceptionResult | None = None,
        plan: ActionPlan | None = None, relationship: RelationshipState | None = None,
        *, examples: bool = False,
    ) -> dict:
        context = self.novel_knowledge.retrieve(text, perception, plan, relationship)
        if not examples:
            context.pop("dialogue_examples", None)
        return context

    @staticmethod
    def _history(history: list[ConversationTurn], limit: int = 4) -> list[dict[str, object]]:
        return [asdict(turn) for turn in history[-limit:]]

    @staticmethod
    def _current_messages(event: AgentEvent) -> tuple[list[dict[str, object]], list[dict[str, int]]]:
        raw = event.payload.get("user_messages") if isinstance(event.payload, dict) else None
        messages: list[dict[str, object]] = []
        if isinstance(raw, list):
            for item in raw[-12:]:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                message: dict[str, object] = {"text": item["text"]}
                if isinstance(item.get("event_id"), (str, int)):
                    message["event_id"] = item["event_id"]
                if isinstance(item.get("timestamp"), (int, float)):
                    message["timestamp"] = item["timestamp"]
                messages.append(message)
        if not messages:
            messages = [{
                "event_id": event.event_id,
                "timestamp": event.timestamp,
                "text": event.text or "",
            }]

        joined_text = event.text or ""
        boundaries: list[dict[str, int]] = []
        cursor = 0
        for index, message in enumerate(messages):
            part = str(message["text"])
            start = joined_text.find(part, cursor) if part else cursor
            if start < 0:
                start = cursor
            end = start + len(part)
            boundaries.append({"index": index, "start": start, "end": end})
            cursor = end
        return messages, boundaries

    @staticmethod
    def _memories(memories: list[MemoryItem]) -> list[dict[str, object]]:
        return [
            {
                "id": item.id,
                "content": item.content,
                "memory_type": item.memory_type,
                "source_layer": item.source_layer,
                "retrieval_score": item.retrieval_score,
                "importance": item.importance,
                "emotional_weight": item.emotional_weight,
                "relationship_weight": item.relationship_weight,
                "confidence": item.confidence,
                "pinned": item.pinned,
            }
            for item in memories
        ]

    @staticmethod
    def _prompt_perception(perception: PerceptionResult) -> dict[str, object]:
        """Project only response-relevant perception fields into model prompts.

        Jev/composite comparison data is diagnostic and must never silently
        become an input to the response model while VAD is the selected source.
        """
        return perception.prompt_values()

    def for_perception(
        self, event: AgentEvent, history: list[ConversationTurn]
    ) -> tuple[str, str]:
        payload = {
            "current_message": event.text or "",
            "recent_history": self._history(history, limit=3),
        }
        return self._read(self._prompt_dir / "perception.txt"), json.dumps(payload, ensure_ascii=False)

    def for_daily_event(
        self, skeleton: dict[str, object], recent_user_text: str | None = None
    ) -> tuple[str, str]:
        payload = {
            "identity": self._read(self._persona_dir / "identity.txt").replace(
                "Alice", self.profile.name
            ),
            "personality": self._read(self._persona_dir / "personality.txt").replace(
                "Alice", self.profile.name
            ),
            "persona_profile": self.profile.prompt_context(),
            "event_skeleton": skeleton,
            "recent_user_text": (recent_user_text or "")[:160],
            "character_memory_boundary": MEMORY_BOUNDARY,
        }
        return self._prompt("daily_event.txt"), json.dumps(payload, ensure_ascii=False)

    def for_daily_event_reaction(
        self, seed: dict[str, object], event_text: str, state: AgentState,
    ) -> tuple[str, str]:
        payload = {
            "event_category": str(seed.get("category") or "")[:40],
            "event_text": event_text[:160],
            "current_mood": {
                key: value for key, value in asdict(state).items()
                if key in {
                    "mood_valence", "mood_arousal", "mood_dominance",
                    "energy", "stress", "loneliness",
                }
            },
        }
        return self._prompt("daily_event_reaction.txt"), json.dumps(payload, ensure_ascii=False)

    def for_planner(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        memories: list[MemoryItem],
        history: list[ConversationTurn],
        relationship: RelationshipState | None = None,
    ) -> tuple[str, str]:
        personality = self._read(self._persona_dir / "personality.txt").replace(
            "Alice", self.profile.name
        )
        current_messages, message_boundaries = self._current_messages(event)
        conversation_style_guidance = _conversation_style_guidance(current_messages, history)
        payload = {
            "event": {"text": event.text or "", "type": event.type},
            "current_messages": current_messages,
            "message_boundaries": message_boundaries,
            "perception": self._prompt_perception(perception),
            "state": asdict(state),
            "relationship": relationship_summary(
                relationship or RelationshipState(event.user_id)
            ),
            "relevant_memories": self._memories(memories),
            "recent_history": self._history(history),
            "conversation_style_guidance": conversation_style_guidance,
            "personality_principles": personality,
            "persona_profile": self.profile.prompt_context(),
            "character_knowledge": self._novel_context(event.text or "", perception, relationship=relationship),
        }
        return self._prompt("planner.txt"), json.dumps(payload, ensure_ascii=False)

    def for_response(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        memories: list[MemoryItem],
        plan: ActionPlan,
        history: list[ConversationTurn],
        relationship: RelationshipState | None = None,
    ) -> tuple[str, str]:
        persona = "\n".join(
            self._read(self._persona_dir / name)
            for name in ("identity.txt", "personality.txt", "speaking_style.txt")
        ).replace("Alice", self.profile.name)
        current_messages, message_boundaries = self._current_messages(event)
        conversation_style_guidance = _conversation_style_guidance(current_messages, history)
        payload = {
            "persona": persona,
            "persona_profile": self.profile.prompt_context(),
            "current_message": event.text or "",
            "current_messages": current_messages,
            "message_boundaries": message_boundaries,
            "perception": self._prompt_perception(perception),
            "state": asdict(state),
            "relationship": relationship_summary(
                relationship or RelationshipState(event.user_id)
            ),
            "action_plan": asdict(plan),
            "selected_memories": self._memories(memories),
            "recent_history": self._history(history),
            "conversation_style_guidance": conversation_style_guidance,
            "character_knowledge": self._novel_context(
                event.text or "", perception, plan, relationship, examples=True,
            ),
        }
        return self._prompt("response.txt"), json.dumps(payload, ensure_ascii=False)

    def for_proactive_planner(
        self,
        candidate: CandidateAction,
        state: AgentState,
        relationship: RelationshipState,
        drives: DriveState,
        memories: list[MemoryItem],
        availability: UserAvailability,
    ) -> tuple[str, str]:
        payload = {
            "candidate": {
                "action_type": candidate.action_type.value,
                "reason": candidate.reason,
                "urgency": candidate.urgency,
                "relevance": candidate.relevance,
                "topic": candidate.topic,
                "context_summary": candidate.metadata.get("summary", ""),
                "daily_event_id": candidate.metadata.get("daily_event_id"),
                "daily_event_day": candidate.metadata.get("daily_event_day"),
                "daily_decision": candidate.metadata.get("daily_decision"),
            },
            "state": asdict(state),
            "relationship": relationship_summary(relationship),
            "drives": {
                "social_need": drives.social_need,
                "curiosity": drives.curiosity,
                "rest_need": drives.rest_need,
            },
            "relevant_memories": self._memories(memories),
            "availability": asdict(availability),
            "persona_profile": self.profile.prompt_context(),
            "character_knowledge": self._novel_context(
                " ".join(str(value or "") for value in (
                    candidate.topic, candidate.reason, candidate.metadata.get("summary"),
                )), relationship=relationship,
            ),
        }
        return self._prompt("proactive_planner.txt"), json.dumps(payload, ensure_ascii=False)
