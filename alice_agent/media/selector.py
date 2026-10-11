from __future__ import annotations

import random
import time
from dataclasses import asdict

from ..decision.models import DecisionMode, DecisionType
from ..decision.schemas import MEDIA_SCHEMA, media_questions
from ..decision.service import DecisionService
from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.perception import PerceptionResult
from ..domain.response import AgentResponse
from ..domain.state import AgentState
from .catalog import MediaCatalog
from .emoji_alignment import candidate_intents, evaluate_emoji, response_intents, response_text_for_alignment
from .models import MediaConfig, MediaDecision, RankedMediaCandidate
from .scoring import media_target, rank_candidates


class DecisionAwareMediaSelector:
    def __init__(
        self,
        catalog: MediaCatalog,
        decisions: DecisionService,
        config: MediaConfig,
        *,
        default_reference_available: bool = False,
        voice_runtime_available: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._catalog = catalog
        self._decisions = decisions
        self._config = config
        self._default_reference_available = default_reference_available
        self._voice_runtime_available = voice_runtime_available
        self._rng = rng or random.Random()
        self._last_trigger: dict[str, float] = {}

    def _rank(
        self,
        event: AgentEvent,
        response: AgentResponse,
        perception: PerceptionResult,
        state: AgentState,
        plan: ActionPlan,
    ) -> tuple[list[RankedMediaCandidate], list[RankedMediaCandidate], dict[str, object]]:
        target_vad, target_energy = media_target(perception, state, plan)
        context = f"{event.text or ''}\n{response.text or ''}\n{plan.emotion} {plan.tone} {plan.expression or ''}"
        audio = rank_candidates(
            self._catalog.audio,
            context_text=context,
            target_vad=target_vad,
            target_energy=target_energy,
            kind="audio",
            limit=self._config.candidate_limit,
        )
        # Emoji eligibility is grounded in the assistant's unquoted reply. User
        # text, plan labels, VAD, and energy cannot make a semantic match by themselves.
        alignment_text = response_text_for_alignment(response.text or "")
        reply_intents = response_intents(response.text or "")
        evaluations = {
            candidate.id: evaluate_emoji(candidate, reply_intents, response.text or "")
            for candidate in self._catalog.emoji
        }
        eligible_candidates = [
            candidate for candidate in self._catalog.emoji
            if evaluations[candidate.id].eligible
        ]
        all_ranked_emoji = rank_candidates(
            eligible_candidates,
            context_text=alignment_text,
            target_vad=target_vad,
            target_energy=target_energy,
            kind="emoji",
            limit=max(1, len(eligible_candidates)),
        )
        ranked_emoji = all_ranked_emoji[:self._config.candidate_limit]
        # The local score threshold is a hard gate for both deterministic and
        # Jev selection; otherwise model selection could revive a rejected item.
        emoji = [item for item in ranked_emoji if item.score >= self._config.emoji_score_threshold]

        scores = {item.candidate.id: round(item.score, 4) for item in all_ranked_emoji}
        eligible_rows = [
            {
                "id": item.candidate.id,
                "eligible": True,
                "reason": evaluations[item.candidate.id].reason,
                "intent": ",".join(evaluations[item.candidate.id].intents[:3]) or "none",
                "score": scores[item.candidate.id],
            }
            for item in all_ranked_emoji
        ]
        excluded_rows = [
            {
                "id": candidate.id,
                "eligible": False,
                "reason": evaluations[candidate.id].reason,
                "intent": ",".join(evaluations[candidate.id].intents[:3]) or "none",
                "score": 0.0,
            }
            for candidate in self._catalog.emoji
            if not evaluations[candidate.id].eligible
        ]
        alignment_diagnostics: dict[str, object] = {
            "emoji_alignment": {
                "basis": "response_only",
                "response_intents": list(reply_intents[:12]),
                "candidate_count": len(self._catalog.emoji),
                "eligible_count": len(eligible_candidates),
                "abstained": not eligible_candidates,
                "candidates": (eligible_rows + excluded_rows)[:12],
            }
        }
        return audio, emoji, alignment_diagnostics

    def _baseline(
        self,
        audio: list[RankedMediaCandidate],
        emoji: list[RankedMediaCandidate],
        diagnostics: dict[str, object],
    ) -> MediaDecision:
        best_audio = audio[0] if audio else None
        best_emoji = emoji[0] if emoji else None
        voice_available = bool(best_audio) or self._default_reference_available
        send_voice = (
            self._config.voice_enabled
            and self._voice_runtime_available
            and voice_available
            and self._probability_passes(self._config.voice_probability)
        )
        send_emoji = (
            self._config.emoji_enabled
            and best_emoji is not None
            and best_emoji.score >= self._config.emoji_score_threshold
            and self._probability_passes(self._config.emoji_probability)
        )
        voice = (
            best_audio.candidate
            if best_audio is not None and best_audio.score >= self._config.audio_score_threshold
            else None
        )
        return MediaDecision(
            send_voice=send_voice,
            send_emoji=send_emoji,
            voice=voice,
            emoji=best_emoji.candidate if send_emoji and best_emoji is not None else None,
            source="deterministic",
            reason="deterministic media policy",
            diagnostics=diagnostics,
        )

    def _probability_passes(self, probability: float) -> bool:
        return probability > 0.0 and self._rng.random() < probability

    @staticmethod
    def _candidate_payload(
        items: list[RankedMediaCandidate], *, include_emoji_metadata: bool = False
    ) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for item in items:
            candidate = item.candidate
            record: dict[str, object] = {
                "id": candidate.id,
                "description": candidate.description,
                "vad": list(candidate.vad) if candidate.vad is not None else None,
                "energy": candidate.energy,
                "local_score": round(item.score, 4),
            }
            if include_emoji_metadata:
                record.update({
                    "caption": candidate.caption[:120],
                    "intent": candidate.intent[:48] or ",".join(candidate_intents(candidate)[:3]),
                    "tags": list(candidate.tags[:12]),
                })
            payload.append(record)
        return payload

    def _remember_trigger(self, session_id: str, decision: MediaDecision, now: float) -> MediaDecision:
        if decision.send_voice or decision.send_emoji:
            self._last_trigger[session_id] = now
        return decision

    async def decide(
        self,
        event: AgentEvent,
        response: AgentResponse,
        perception: PerceptionResult,
        state: AgentState,
        plan: ActionPlan,
    ) -> MediaDecision:
        text = (response.text or "").strip()
        if not self._config.enabled or len(text) < self._config.min_chars:
            return MediaDecision(reason="media disabled or response too short")
        if bool(response.metadata.get("fallback")):
            return MediaDecision(reason="do not decorate fallback responses")
        now = time.monotonic()
        previous = self._last_trigger.get(event.session_id, 0.0)
        if self._config.cooldown_seconds and now - previous < self._config.cooldown_seconds:
            return MediaDecision(reason="media cooldown active")

        audio, emoji, diagnostics = self._rank(event, response, perception, state, plan)
        baseline = self._baseline(audio, emoji, diagnostics)
        if not baseline.send_voice and not baseline.send_emoji:
            return baseline
        if not self._config.jev_enabled or not self._decisions.enabled:
            return self._remember_trigger(event.session_id, baseline, now)

        questions = media_questions(
            [item.candidate.id for item in audio] if self._config.voice_enabled and self._voice_runtime_available else [],
            [item.candidate.id for item in emoji] if self._config.emoji_enabled else [],
            voice_available=self._config.voice_enabled and self._voice_runtime_available and (
                bool(audio) or self._default_reference_available
            ),
            emoji_available=self._config.emoji_enabled and bool(emoji),
        )
        state_payload = {
            "event": {"text": (event.text or "")[:1000], "type": event.event_type or event.type},
            "response": text[:1000],
            "perception": perception.prompt_values(),
            "agent_state": asdict(state),
            "action_plan": {
                "action": plan.action,
                "emotion": plan.emotion,
                "energy": plan.energy,
                "expression": plan.expression,
                "voice_style": plan.voice_style,
                "tone": plan.tone,
                "directness": plan.directness,
            },
            "audio_candidates": self._candidate_payload(audio),
            "emoji_candidates": self._candidate_payload(emoji, include_emoji_metadata=True),
        }
        result = await self._decisions.decide(
            state_payload,
            questions,
            decision_type=DecisionType.MEDIA.value,
            schema_version=MEDIA_SCHEMA,
            trace_id=event.trace_id,
        )
        if result is None:
            return self._remember_trigger(event.session_id, baseline, now)

        audio_ranked_by_id = {item.candidate.id: item for item in audio}
        emoji_by_id = {item.candidate.id: item.candidate for item in emoji}
        send_voice = float(result.answers.get("send_voice").noul or 0.0) >= 0.65 if "send_voice" in result.answers else False
        send_emoji = float(result.answers.get("send_emoji").noul or 0.0) >= 0.65 if "send_emoji" in result.answers else False
        voice_choice = result.answers.get("voice_choice")
        emoji_choice = result.answers.get("emoji_choice")
        voice_ranked = audio_ranked_by_id.get(voice_choice.choice) if voice_choice is not None else None
        voice = (
            voice_ranked.candidate
            if voice_ranked is not None and voice_ranked.score >= self._config.audio_score_threshold
            else None
        )
        emoji_item = emoji_by_id.get(emoji_choice.choice) if emoji_choice is not None else None
        deep = float(result.answers["requires_deep_reasoning"].noul or 0.0) >= 0.5
        if deep:
            mapped = baseline
        else:
            mapped = MediaDecision(
                send_voice=(
                    send_voice and baseline.send_voice
                    and (voice is not None or self._default_reference_available)
                ),
                send_emoji=send_emoji and baseline.send_emoji and emoji_item is not None,
                voice=voice,
                emoji=emoji_item,
                source="jev",
                reason="Jev bounded media decision",
                diagnostics=baseline.diagnostics,
            )
        if self._decisions.mode == DecisionMode.SHADOW:
            self._decisions.record_comparison(
                DecisionType.MEDIA.value,
                event.trace_id,
                (mapped.send_voice, mapped.voice.id if mapped.voice else None,
                 mapped.send_emoji, mapped.emoji.id if mapped.emoji else None),
                (baseline.send_voice, baseline.voice.id if baseline.voice else None,
                 baseline.send_emoji, baseline.emoji.id if baseline.emoji else None),
            )
            return self._remember_trigger(event.session_id, MediaDecision(
                send_voice=baseline.send_voice,
                send_emoji=baseline.send_emoji,
                voice=baseline.voice,
                emoji=baseline.emoji,
                source="shadow",
                reason="shadow mode keeps deterministic media policy",
                diagnostics=baseline.diagnostics,
            ), now)
        if self._decisions.policy.may_use_model_decision(self._decisions.mode, result.confidence):
            return self._remember_trigger(event.session_id, mapped, now)
        return self._remember_trigger(event.session_id, baseline, now)
