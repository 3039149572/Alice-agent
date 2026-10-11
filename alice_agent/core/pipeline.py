from __future__ import annotations

import math
import time
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from ..config import AgentConfig
from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.perception import PerceptionResult
from ..domain.response import AgentResponse
from ..domain.state import AgentState
from ..generation.response_generator import ResponseGenerator
from ..memory.manager import MemoryManager
from ..media.selector import DecisionAwareMediaSelector
from ..perception.service import PerceptionService
from ..planner.service import PlannerService
from ..ports.logging import LoggerPort
from ..relationship.manager import RelationshipManager
from ..relationship.models import RelationshipState, relationship_summary
from ..state.manager import StateManager
from .session_manager import SessionManager


class AgentPipeline:
    def __init__(
        self,
        *,
        config: AgentConfig,
        perception: PerceptionService,
        state_manager: StateManager,
        memory: MemoryManager,
        planner: PlannerService,
        response_generator: ResponseGenerator,
        session_manager: SessionManager,
        logger: LoggerPort,
        relationship_manager: RelationshipManager | None = None,
        media_selector: DecisionAwareMediaSelector | None = None,
        daily_events: object | None = None,
    ) -> None:
        self._config = config
        self._perception = perception
        self._state_manager = state_manager
        self._memory = memory
        self._planner = planner
        self._response_generator = response_generator
        self._session_manager = session_manager
        self._logger = logger
        self._relationship = relationship_manager
        self._media_selector = media_selector
        self._daily_events = daily_events

    def _detail(self, trace_id: str, stage: str, value: Any) -> None:
        if self._config.debug:
            rendered = repr(value)
            self._logger.debug(f"[{trace_id}] {stage}: {rendered[:2000]}")

    @staticmethod
    def _memory_diagnostics(items: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "memory_type": item.memory_type,
                "source_layer": item.source_layer,
                "content_excerpt": item.content[:1000],
                "retrieval_score": item.retrieval_score,
                "importance": item.importance,
                "emotional_weight": item.emotional_weight,
                "relationship_weight": item.relationship_weight,
                "confidence": item.confidence,
                "pinned": item.pinned,
            }
            for item in items
        ]

    @staticmethod
    def _media_diagnostics(media: Any) -> dict[str, Any] | None:
        if media is None:
            return None

        def candidate(item: Any) -> dict[str, Any] | None:
            if item is None:
                return None
            return {
                "id": getattr(item, "id", None),
                "kind": getattr(item, "kind", None),
                "description": getattr(item, "description", None),
            }

        result = {
            "send_voice": bool(getattr(media, "send_voice", False)),
            "send_emoji": bool(getattr(media, "send_emoji", False)),
            "source": getattr(media, "source", "unknown"),
            "reason": getattr(media, "reason", ""),
            "voice": candidate(getattr(media, "voice", None)),
            "emoji": candidate(getattr(media, "emoji", None)),
        }
        diagnostics = getattr(media, "diagnostics", None)
        if isinstance(diagnostics, dict):
            alignment = diagnostics.get("emoji_alignment")
            safe_alignment = AgentPipeline._emoji_alignment_diagnostics(alignment)
            if safe_alignment is not None:
                result["diagnostics"] = {"emoji_alignment": safe_alignment}
        return result

    @staticmethod
    def _emoji_alignment_diagnostics(value: Any) -> dict[str, Any] | None:
        """Keep only the bounded, non-text emoji selection explanation."""
        if not isinstance(value, dict):
            return None

        safe: dict[str, Any] = {}
        basis = value.get("basis")
        if isinstance(basis, str):
            safe["basis"] = basis[:32]

        intents = value.get("response_intents")
        if isinstance(intents, (list, tuple)):
            safe["response_intents"] = [
                item[:32] for item in intents[:8] if isinstance(item, str)
            ]

        for key in ("candidate_count", "eligible_count"):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool):
                safe[key] = min(1_000_000, max(0, count))

        abstained = value.get("abstained")
        if isinstance(abstained, bool):
            safe["abstained"] = abstained

        candidates = value.get("candidates")
        if isinstance(candidates, (list, tuple)):
            safe_candidates: list[dict[str, Any]] = []
            for item in candidates[:12]:
                if not isinstance(item, dict):
                    continue
                safe_item: dict[str, Any] = {}
                for key, limit in (("id", 80), ("reason", 48), ("intent", 32)):
                    text = item.get(key)
                    if isinstance(text, str):
                        safe_item[key] = text[:limit]
                eligible = item.get("eligible")
                if isinstance(eligible, bool):
                    safe_item["eligible"] = eligible
                score = item.get("score")
                if isinstance(score, (int, float)) and not isinstance(score, bool):
                    numeric_score = float(score)
                    if math.isfinite(numeric_score):
                        safe_item["score"] = min(1.0, max(0.0, numeric_score))
                if safe_item:
                    safe_candidates.append(safe_item)
            safe["candidates"] = safe_candidates

        return safe or None

    async def handle_event(
        self,
        event: AgentEvent,
        *,
        is_current: Callable[[], Awaitable[bool]] | None = None,
    ) -> AgentResponse:
        started = time.perf_counter()
        trace = event.trace_id
        self._logger.info(
            f"[{trace}] EVENT RECEIVED source={event.source} type={event.type} "
            f"session={event.session_id} text_length={len(event.text or '')}"
        )
        async with self._session_manager.lock(event.session_id):
            if not await self._is_current(is_current):
                return self._superseded_response(event)
            history = await self._session_manager.load_history(event.session_id)
            today_events: list[dict[str, Any]] = []
            if self._daily_events is not None:
                try:
                    today_events = await self._daily_events.ensure(
                        event.session_id, event.user_id, event.timestamp, history
                    )
                except Exception:
                    self._logger.exception(f"[{trace}] ERROR stage=DAILY EVENTS")
            if not await self._is_current(is_current):
                return self._superseded_response(event)

            try:
                state_before = await self._state_manager.get(event.session_id)
            except Exception:
                self._logger.exception(f"[{trace}] ERROR stage=STATE BEFORE")
                state_before = AgentState()
            self._logger.info(
                f"[{trace}] STATE BEFORE valence={state_before.mood_valence:.3f} "
                f"arousal={state_before.mood_arousal:.3f} energy={state_before.energy:.3f}"
            )
            self._detail(trace, "STATE BEFORE", asdict(state_before))

            try:
                perception = await self._perception.analyze(event, history)
            except Exception:
                self._logger.exception(f"[{trace}] ERROR stage=PERCEPTION")
                perception = PerceptionResult.fallback()
            if not await self._is_current(is_current):
                return self._superseded_response(event)
            self._logger.info(
                f"[{trace}] PERCEPTION confidence={perception.confidence:.3f} "
                f"importance={perception.importance:.3f} candidate={perception.memory_candidate}"
            )
            self._detail(trace, "PERCEPTION", asdict(perception))

            try:
                state_after = await self._state_manager.preview_update(event, perception)
            except Exception:
                self._logger.exception(f"[{trace}] ERROR stage=STATE AFTER")
                state_after = state_before
            self._logger.info(
                f"[{trace}] STATE AFTER valence={state_after.mood_valence:.3f} "
                f"arousal={state_after.mood_arousal:.3f} energy={state_after.energy:.3f}"
            )
            self._detail(trace, "STATE AFTER", asdict(state_after))
            if not await self._is_current(is_current):
                return self._superseded_response(event)

            relationship = RelationshipState(user_id=event.user_id)
            if self._relationship is not None:
                try:
                    relationship = await self._relationship.get(event.user_id)
                except Exception:
                    self._logger.exception(f"[{trace}] ERROR stage=RELATIONSHIP")
                    try:
                        relationship = await self._relationship.get(event.user_id)
                    except Exception:
                        relationship = RelationshipState(user_id=event.user_id)
            self._logger.info(
                f"[{trace}] RELATIONSHIP interactions={relationship.interaction_count}"
            )
            self._detail(
                trace, "RELATIONSHIP", relationship_summary(relationship)
            )

            try:
                memories = await self._memory.recall(
                    event, perception, state_after, relationship
                )
            except Exception:
                self._logger.exception(f"[{trace}] ERROR stage=MEMORY RETRIEVED")
                memories = []
            if not await self._is_current(is_current):
                return self._superseded_response(event)
            self._logger.info(f"[{trace}] MEMORY RETRIEVED count={len(memories)}")
            self._detail(trace, "MEMORY RETRIEVED DETAIL", [item.id for item in memories])

            try:
                plan = await self._planner.plan(
                    event, perception, state_after, memories, history, relationship
                )
            except Exception:
                self._logger.exception(f"[{trace}] ERROR stage=ACTION PLAN")
                plan = ActionPlan.fallback()
            if not await self._is_current(is_current):
                return self._superseded_response(event)
            planner_comparison = None
            pop_comparison = getattr(self._planner, "pop_comparison", None)
            if callable(pop_comparison):
                planner_comparison = pop_comparison(trace)
            planner_silence_action = None
            if plan.action in {"ignore", "wait"} and event.metadata.get("timing_force_reply"):
                # Timing decisions belong to ResponseTimingService. Planner's
                # legacy silence actions must not strand a timed-out question.
                planner_silence_action = plan.action
                plan.action = "acknowledge"
                plan.response_goal = "简短、自然地回应当前消息，不等待下一条消息"
            self._logger.info(
                f"[{trace}] ACTION PLAN action={plan.action} selected_memories={len(plan.memory_ids)}"
            )
            self._detail(trace, "ACTION PLAN", asdict(plan))

            selected_memories: list = []
            try:
                selected_memories = self._memory.select_for_response(
                    memories, plan.memory_ids
                )
                plan.memory_ids = [item.id for item in selected_memories]
            except AttributeError:
                selected = set(plan.memory_ids)
                selected_memories = [item for item in memories if item.id in selected]
            except Exception:
                self._logger.exception(f"[{trace}] ERROR stage=MEMORY SELECTION")
                selected_memories = []
                plan.memory_ids = []
            self._logger.info(
                f"[{trace}] MEMORY SELECTED count={len(selected_memories)}"
            )

            response_failed = False
            try:
                response = await self._response_generator.generate(
                    event, perception, state_after, selected_memories, plan,
                    history, relationship
                )
            except Exception:
                response_failed = True
                self._logger.exception(f"[{trace}] ERROR stage=LLM RESPONSE")
                response = AgentResponse(
                    text=self._config.fallback_text,
                    trace_id=trace,
                    action=plan.action,
                    metadata={"fallback": True},
                )
            if not await self._is_current(is_current):
                return self._superseded_response(event)
            self._logger.info(
                f"[{trace}] LLM RESPONSE action={response.action} "
                f"text_length={len(response.text or '')} fallback={response_failed}"
            )
            if planner_silence_action is not None:
                response.metadata["planner_silence_action"] = planner_silence_action

            if self._media_selector is not None and response.text:
                try:
                    response.metadata["media_decision"] = await self._media_selector.decide(
                        event, response, perception, state_after, plan
                    )
                    media = response.metadata["media_decision"]
                    self._logger.info(
                        f"[{trace}] MEDIA DECISION source={media.source} "
                        f"voice={media.send_voice} emoji={media.send_emoji}"
                    )
                except Exception:
                    self._logger.exception(f"[{trace}] ERROR stage=MEDIA DECISION")
            if not await self._is_current(is_current):
                return self._superseded_response(event)

            response.metadata["turn_diagnostics"] = {
                "history_before_turn": [asdict(turn) for turn in history],
                "response_timing": (
                    event.metadata.get("response_timing")
                    if isinstance(event.metadata, dict) else None
                ),
                "perception": perception.prompt_values(),
                "emotion_comparison": perception.emotion_comparison,
                "decision_comparison": {
                    "perception": (
                        perception.emotion_comparison.get("decision_comparison")
                        if isinstance(perception.emotion_comparison, dict) else None
                    ),
                    "planner": planner_comparison,
                },
                "state_before": asdict(state_before),
                "daily_events": today_events,
                "state_after": asdict(state_after),
                "relationship": relationship_summary(relationship),
                "memory_recall": self._memory_diagnostics(memories),
                "memory_selected": self._memory_diagnostics(selected_memories),
                "action_plan": asdict(plan),
                "response": {
                    "action": response.action,
                    "fallback": response_failed or bool(response.metadata.get("fallback")),
                },
                "media_decision": self._media_diagnostics(
                    response.metadata.get("media_decision")
                ),
                "memory_observe_accepted": None,
            }
            response.metadata["_alice_pending_commit"] = {
                "perception": perception,
                "state": state_after,
                "plan": plan,
                "selected_memories": selected_memories,
                "relationship": relationship,
            }
            if is_current is None:
                await self._commit_delivery_locked(event, response, response.text or "")

        elapsed_ms = (time.perf_counter() - started) * 1000
        self._logger.info(f"[{trace}] TOTAL LATENCY ms={elapsed_ms:.1f}")
        return response

    @staticmethod
    def _superseded_response(event: AgentEvent) -> AgentResponse:
        return AgentResponse(
            text=None,
            trace_id=event.trace_id,
            action="superseded",
            metadata={"superseded": True},
        )

    async def _is_current(
        self, guard: Callable[[], Awaitable[bool]] | None
    ) -> bool:
        if guard is None:
            return True
        try:
            return bool(await guard())
        except Exception:
            self._logger.exception("Alice reactive revision check failed safely")
            return False

    async def _commit_delivery_locked(
        self, event: AgentEvent, response: AgentResponse, delivered_text: str
    ) -> None:
        pending = response.metadata.pop("_alice_pending_commit", None)
        if not isinstance(pending, dict):
            if response.metadata.get("_alice_delivery_committed"):
                return
            response.metadata["_alice_delivery_committed"] = True
            await self._session_manager.append_and_persist(
                event.session_id,
                ConversationTurn(
                    user_text=event.text or "",
                    assistant_text=delivered_text or None,
                    timestamp=event.timestamp,
                ),
            )
            return
        response.metadata["_alice_delivery_committed"] = True
        perception = pending["perception"]
        state = pending["state"]
        plan = pending["plan"]
        selected_memories = pending["selected_memories"]
        try:
            # AgentLoop time/mood maintenance may run after generation and
            # before delivery; fold this user turn into the latest state.
            state = await self._state_manager.update(event, perception)
        except Exception:
            self._logger.exception(f"[{event.trace_id}] ERROR stage=STATE COMMIT")
        relationship = pending["relationship"]
        if self._relationship is not None:
            try:
                relationship = await self._relationship.update(event, perception)
            except Exception:
                self._logger.exception(f"[{event.trace_id}] ERROR stage=RELATIONSHIP COMMIT")
        try:
            await self._memory.record_access(selected_memories)
        except AttributeError:
            pass
        except Exception:
            self._logger.exception(f"[{event.trace_id}] ERROR stage=MEMORY ACCESS")
        committed_response = AgentResponse(
            text=delivered_text or None,
            trace_id=response.trace_id,
            action=response.action,
            metadata={key: value for key, value in response.metadata.items() if key != "turn_diagnostics"},
        )
        observed = False
        try:
            observed = await self._memory.observe(
                event, perception, state, plan, committed_response, relationship
            )
        except Exception:
            self._logger.exception(f"[{event.trace_id}] ERROR stage=MEMORY OBSERVE")
        diagnostics = response.metadata.get("turn_diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics["memory_observe_accepted"] = observed
            diagnostics["delivered_text_length"] = len(delivered_text)
        await self._session_manager.append_and_persist(
            event.session_id,
            ConversationTurn(
                user_text=event.text or "",
                assistant_text=delivered_text or None,
                timestamp=event.timestamp,
            ),
        )

    async def commit_delivery(
        self, event: AgentEvent, response: AgentResponse, delivered_text: str
    ) -> None:
        async with self._session_manager.lock(event.session_id):
            await self._commit_delivery_locked(event, response, delivered_text)

    async def record_suppressed(
        self, event: AgentEvent, diagnostics: dict[str, object]
    ) -> None:
        async with self._session_manager.lock(event.session_id):
            await self._session_manager.append_and_persist(
                event.session_id,
                ConversationTurn(
                    user_text=event.text or "",
                    assistant_text=None,
                    timestamp=event.timestamp,
                ),
            )

    async def close(self) -> None:
        await self._memory.close()
        await self._state_manager.close()
        if self._relationship is not None:
            await self._relationship.close()
        await self._session_manager.close()
