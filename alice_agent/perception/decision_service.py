from __future__ import annotations

import math
import time
from typing import Any

from ..decision.models import DecisionMode, DecisionResult, DecisionType
from ..decision.schemas import PERCEPTION_SCHEMA, perception_questions
from ..decision.service import DecisionService
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.perception import PerceptionResult, SOCIAL_SIGNAL_KEYS
from .vad import VADModelPort, VADModelScore


_EMOTION_STATE = {
    "positive": (0.55, 0.45), "neutral": (0.0, 0.2), "negative": (-0.45, 0.45),
    "sad": (-0.7, 0.35), "annoyed": (-0.55, 0.7), "excited": (0.75, 0.85),
    "tired": (-0.2, 0.15), "anxious": (-0.45, 0.8), "playful": (0.55, 0.65),
    "uncertain": (-0.1, 0.35),
}

_SCORE_KEYS = (
    "vad_valence", "vad_arousal", "vad_dominance", "emotion_intensity",
    "warmth", "tension", "suppression",
)


class DecisionAwarePerceptionService:
    """Keep bounded Jev decisions and combine available affect estimates."""

    NUMERIC_BLEND_THRESHOLD = 0.80

    def __init__(
        self,
        existing: object,
        decisions: DecisionService,
        *,
        enabled: bool = True,
        vad_model: VADModelPort | None = None,
        vad_enabled: bool = True,
        jev_weight: float = 0.5,
    ) -> None:
        self._existing = existing
        self._decisions = decisions
        self._enabled = enabled
        self._vad_model = vad_model
        self._vad_enabled = bool(vad_enabled)
        self._jev_weight = min(1.0, max(0.0, float(jev_weight)))

    @staticmethod
    def _state(event: AgentEvent, history: list[ConversationTurn]) -> dict:
        return {
            "current_message": (event.text or "")[:2000],
            "recent_context_summary": [
                {
                    "user": turn.user_text[:240],
                    "assistant": (turn.assistant_text or "")[:240],
                }
                for turn in history[-3:]
            ],
            "scene": {"source": event.source, "event_type": event.event_type or event.type},
        }

    @staticmethod
    def _normalized_score(result: DecisionResult, key: str) -> float | None:
        answer = result.answers.get(key)
        if answer is None or answer.score is None:
            return None
        raw_score = float(answer.score)
        if not math.isfinite(raw_score) or not 0.0 <= raw_score <= 4.0:
            return None
        return raw_score / 4.0

    @classmethod
    def _semantic_scores(cls, result: DecisionResult) -> dict[str, Any]:
        scores = {
            key.removeprefix("vad_"): cls._normalized_score(result, key)
            for key in _SCORE_KEYS[:3]
        }
        attributes = {
            key: cls._normalized_score(result, key)
            for key in _SCORE_KEYS[3:]
        }
        emotion = result.answers.get("emotion")
        target = result.answers.get("emotion_target")
        shift = result.answers.get("emotion_shift")
        response_need = result.answers.get("needs_emotional_response")
        return {
            "scores": scores,
            "attributes": attributes,
            "emotion_label": emotion.choice if emotion else None,
            "emotion_label_distribution": dict(emotion.probabilities) if emotion else {},
            "emotion_target": target.choice if target else None,
            "emotion_shift": shift.choice if shift else None,
            "needs_emotional_response": response_need.noul if response_need else None,
        }

    @classmethod
    def _map(cls, result: DecisionResult) -> PerceptionResult:
        intent = result.answers["intent"].choice or "other"
        emotion = result.answers["emotion"].choice or "neutral"
        valence, arousal = _EMOTION_STATE.get(emotion, (0.0, 0.2))
        signals = {
            key: float(result.answers[f"social_{key}"].noul or 0.0)
            for key in SOCIAL_SIGNAL_KEYS
        }
        relationship_event = result.answers["relationship_event"].choice
        if relationship_event == "relationship_positive":
            signals["affection"] = max(signals["affection"], 0.55)
        elif relationship_event in {"relationship_negative", "conflict"}:
            signals["conflict"] = max(signals["conflict"], 0.55)
        importance_answer = result.answers["importance"]
        importance_score = float(importance_answer.score or 0.0)
        importance = min(1.0, max(0.0, importance_score / 4.0))
        vad = cls._semantic_scores(result)["scores"]
        # These mapped values are a fallback; the final affect is assembled
        # from validated Jev/LLM scores and the local VAD after mapping.
        if vad["valence"] is not None:
            valence = float(vad["valence"]) * 2.0 - 1.0
        if vad["arousal"] is not None:
            arousal = float(vad["arousal"])
        return PerceptionResult(
            intent=intent,
            topic="general",
            user_emotion=emotion,
            valence=valence,
            arousal=arousal,
            dominance=float(vad["dominance"] if vad["dominance"] is not None else 0.5),
            social_signals=signals,
            importance=importance,
            memory_candidate=float(result.answers["memory_worthy"].noul or 0.0) >= 0.65,
            confidence=result.confidence,
        )

    @staticmethod
    def _emotion_from_vad(valence_01: float, arousal: float) -> str:
        """Coarse response label derived from the selected affect scores."""
        if valence_01 <= 0.32:
            if arousal <= 0.30:
                return "sad"
            if arousal >= 0.70:
                return "anxious"
            return "negative"
        if valence_01 >= 0.68:
            return "excited" if arousal >= 0.70 else "positive"
        if arousal <= 0.25:
            return "tired"
        return "neutral"

    @staticmethod
    def _llm_has_number(perception: PerceptionResult | None, key: str) -> bool:
        if perception is None:
            return False
        evidence = perception.numeric_evidence
        return key in evidence if evidence is not None else perception.confidence > 0.0

    def _low_confidence_weight(self, result: DecisionResult, answer_key: str) -> float:
        answer = result.answers.get(answer_key)
        if answer is None:
            return 0.0
        confidence = min(result.confidence, answer.effective_confidence)
        return 0.5 * min(1.0, max(0.0, confidence / self.NUMERIC_BLEND_THRESHOLD))

    async def _vad_attempt(
        self, event: AgentEvent
    ) -> tuple[VADModelScore | None, dict[str, Any]]:
        if not self._vad_enabled:
            return None, {"status": "disabled", "elapsed_ms": 0.0, "error_kind": None}
        if self._vad_model is None:
            return None, {"status": "unavailable", "elapsed_ms": 0.0, "error_kind": "adapter_missing"}
        if not event.text:
            return None, {"status": "skipped", "elapsed_ms": 0.0, "error_kind": "empty_text"}
        started = time.perf_counter()
        try:
            score = await self._vad_model.score(event.text)
        except Exception as exc:
            return None, {
                "status": "error",
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                "error_kind": type(exc).__name__,
            }
        try:
            raw = tuple(float(value) for value in score.raw)
            normalized = tuple(float(value) for value in score.normalized)
            if len(raw) != 3 or len(normalized) != 3:
                raise ValueError("wrong_axis_count")
            if any(not math.isfinite(value) or value < 1.0 or value > 5.0 for value in raw):
                raise ValueError("invalid_raw_axis")
            if any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in normalized):
                raise ValueError("invalid_normalized_axis")
            reported = {
                str(key): float(value)
                for key, value in score.reported_axes.items()
                if math.isfinite(float(value))
            }
            score = VADModelScore(raw=raw, normalized=normalized, reported_axes=reported)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None, {
                "status": "error",
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                "error_kind": "invalid_vad_score",
            }
        return score, {
            "status": "ok",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            "error_kind": None,
        }

    async def analyze(
        self, event: AgentEvent, history: list[ConversationTurn]
    ) -> PerceptionResult:
        jev_attempt: dict[str, Any] = {}
        jev_result: DecisionResult | None = None
        jev_mapped: PerceptionResult | None = None
        base: PerceptionResult | None = None
        jev_selected = False
        jev_would_skip_llm = False
        existing_perception_affect: dict[str, Any] | None = None
        llm_decision: dict[str, Any] | None = None
        llm_perception: PerceptionResult | None = None
        llm_error_kind: str | None = None

        if self._enabled:
            jev_result = await self._decisions.decide(
                self._state(event, history), perception_questions(),
                decision_type=DecisionType.PERCEPTION.value,
                schema_version=PERCEPTION_SCHEMA,
                trace_id=event.trace_id,
                attempt_diagnostics=jev_attempt,
            )
        else:
            jev_attempt = {
                "status": "disabled_by_config", "cached": False,
                "api_latency_ms": None, "lookup_elapsed_ms": 0.0,
                "error_kind": None,
            }

        if jev_result is None:
            if self._enabled and self._decisions.enabled:
                self._decisions.record_escalation(DecisionType.PERCEPTION.value)
            base = await self._existing.analyze(event, history)
            llm_perception = base
            llm_decision = base.prompt_values()
            existing_perception_affect = {
                "user_emotion": base.user_emotion,
                "valence": base.valence,
                "arousal": base.arousal,
                "dominance": base.dominance,
            }
        else:
            jev_mapped = self._map(jev_result)
            deep = float(jev_result.answers["requires_deep_reasoning"].noul or 0.0) >= 0.5
            if self._decisions.mode == DecisionMode.SHADOW:
                base = await self._existing.analyze(event, history)
                llm_perception = base
                llm_decision = base.prompt_values()
                existing_perception_affect = {
                    "user_emotion": base.user_emotion,
                    "valence": base.valence,
                    "arousal": base.arousal,
                    "dominance": base.dominance,
                }
                self._decisions.record_comparison(
                    DecisionType.PERCEPTION.value,
                    event.trace_id,
                    (jev_mapped.intent, jev_mapped.memory_candidate),
                    (base.intent, base.memory_candidate),
                )
            elif not deep and self._decisions.policy.may_use_model_decision(
                self._decisions.mode, jev_result.confidence
            ):
                base = jev_mapped
                jev_selected = True
                jev_would_skip_llm = True
            else:
                self._decisions.record_escalation(DecisionType.PERCEPTION.value)
                base = await self._existing.analyze(event, history)
                llm_perception = base
                llm_decision = base.prompt_values()
                existing_perception_affect = {
                    "user_emotion": base.user_emotion,
                    "valence": base.valence,
                    "arousal": base.arousal,
                    "dominance": base.dominance,
                }

        numeric_answer_keys = (
            *(f"vad_{axis}" for axis in ("valence", "arousal", "dominance")),
            "importance",
            *(f"social_{signal}" for signal in SOCIAL_SIGNAL_KEYS),
        )
        numeric_needs_llm = bool(
            jev_result is not None and any(
                min(jev_result.confidence, jev_result.answers[key].effective_confidence)
                < self.NUMERIC_BLEND_THRESHOLD
                for key in numeric_answer_keys if key in jev_result.answers
            )
        )
        if numeric_needs_llm and llm_perception is None:
            self._decisions.record_escalation(DecisionType.PERCEPTION.value)
            jev_would_skip_llm = False
            try:
                llm_perception = await self._existing.analyze(event, history)
            except Exception as exc:
                # A numeric-only escalation must not discard a valid local VAD.
                llm_error_kind = type(exc).__name__
            else:
                llm_decision = llm_perception.prompt_values()
                existing_perception_affect = {
                    "user_emotion": llm_perception.user_emotion,
                    "valence": llm_perception.valence,
                    "arousal": llm_perception.arousal,
                    "dominance": llm_perception.dominance,
                }

        vad_score, vad_attempt = await self._vad_attempt(event)
        if vad_score is None and jev_selected:
            # Jev may still supply intent/social decisions, but when the VAD
            # service is unavailable the affect fallback is the existing LLM.
            fallback = llm_perception or await self._existing.analyze(event, history)
            llm_perception = fallback
            llm_decision = fallback.prompt_values()
            existing_perception_affect = {
                "user_emotion": fallback.user_emotion,
                "valence": fallback.valence,
                "arousal": fallback.arousal,
                "dominance": fallback.dominance,
            }
            base.user_emotion = fallback.user_emotion
            base.valence = fallback.valence
            base.arousal = fallback.arousal
            base.dominance = fallback.dominance
        elif vad_score is not None and jev_would_skip_llm:
            self._decisions.record_llm_avoided(DecisionType.PERCEPTION.value)

        jev_semantic = self._semantic_scores(jev_result) if jev_result is not None else None
        vad_scores = (
            dict(zip(("valence", "arousal", "dominance"), vad_score.normalized))
            if vad_score is not None else None
        )
        jev_scores = jev_semantic["scores"] if jev_semantic is not None else None
        jev_raw_scores = (
            {
                key.removeprefix("vad_"): jev_result.answers[key].score
                for key in _SCORE_KEYS[:3]
            }
            if jev_result is not None else None
        )
        llm_scores: dict[str, float] | None = None
        if llm_perception is not None:
            llm_scores = {
                "valence": (llm_perception.valence + 1.0) / 2.0,
                "arousal": llm_perception.arousal,
                "dominance": llm_perception.dominance,
            }
        jev_llm_scores: dict[str, float] = {}
        numeric_weights: dict[str, float] = {}
        effective_jev_scores: dict[str, float] = {}
        if jev_result is not None and jev_scores is not None:
            for axis in ("valence", "arousal", "dominance"):
                jev_value = jev_scores.get(axis)
                if jev_value is None:
                    continue
                axis_is_low = (
                    min(jev_result.confidence, jev_result.answers[f"vad_{axis}"].effective_confidence)
                    < self.NUMERIC_BLEND_THRESHOLD
                )
                if axis_is_low:
                    if llm_scores is None or not self._llm_has_number(llm_perception, axis):
                        continue
                    weight = self._low_confidence_weight(jev_result, f"vad_{axis}")
                    numeric_weights[axis] = weight
                    effective_jev_scores[axis] = (
                        (1.0 - weight) * llm_scores[axis]
                        + weight * float(jev_value)
                    )
                    jev_llm_scores[axis] = effective_jev_scores[axis]
                else:
                    effective_jev_scores[axis] = float(jev_value)

        if numeric_needs_llm and jev_result is not None and llm_perception is not None:
            for signal in SOCIAL_SIGNAL_KEYS:
                answer = jev_result.answers.get(f"social_{signal}")
                if answer is None or answer.noul is None or not self._llm_has_number(
                    llm_perception, f"social_{signal}"
                ):
                    continue
                if min(jev_result.confidence, answer.effective_confidence) >= self.NUMERIC_BLEND_THRESHOLD:
                    continue
                weight = self._low_confidence_weight(jev_result, f"social_{signal}")
                base.social_signals[signal] = (
                    (1.0 - weight) * llm_perception.social_signals[signal]
                    + weight * float(answer.noul)
                )
                numeric_weights[f"social_{signal}"] = weight
            importance = self._normalized_score(jev_result, "importance")
            importance_answer = jev_result.answers.get("importance")
            if (
                importance is not None
                and importance_answer is not None
                and min(jev_result.confidence, importance_answer.effective_confidence)
                < self.NUMERIC_BLEND_THRESHOLD
                and self._llm_has_number(llm_perception, "importance")
            ):
                weight = self._low_confidence_weight(jev_result, "importance")
                base.importance = (
                    (1.0 - weight) * llm_perception.importance + weight * importance
                )
                numeric_weights["importance"] = weight

        combined_scores: dict[str, float] | None = None
        selected_source = "existing_perception"
        if vad_scores is not None:
            combined_scores = {
                axis: (1.0 - self._jev_weight) * vad_scores[axis]
                + self._jev_weight * effective_jev_scores[axis]
                if axis in effective_jev_scores else vad_scores[axis]
                for axis in ("valence", "arousal", "dominance")
            }
            selected_source = (
                "vad_jev_llm_blend" if jev_llm_scores
                else "vad_jev_blend" if effective_jev_scores else "vad_model"
            )
            combined_status = selected_source
        elif jev_llm_scores and llm_scores is not None:
            combined_scores = {
                axis: effective_jev_scores.get(axis, llm_scores[axis])
                for axis in ("valence", "arousal", "dominance")
            }
            selected_source = "jev_llm_blend"
            combined_status = selected_source
        else:
            combined_status = "llm_fallback" if llm_perception is not None else "unavailable"

        if combined_scores is not None:
            base.valence = combined_scores["valence"] * 2.0 - 1.0
            base.arousal = combined_scores["arousal"]
            base.dominance = combined_scores["dominance"]
            base.user_emotion = self._emotion_from_vad(
                combined_scores["valence"], combined_scores["arousal"]
            )

        jev_diag: dict[str, Any] = {
            **jev_attempt,
            "scores": jev_scores,
            "attributes": jev_semantic["attributes"] if jev_semantic else None,
            "emotion_label": jev_semantic["emotion_label"] if jev_semantic else None,
            "emotion_label_distribution": jev_semantic["emotion_label_distribution"] if jev_semantic else None,
            "emotion_target": jev_semantic["emotion_target"] if jev_semantic else None,
            "emotion_shift": jev_semantic["emotion_shift"] if jev_semantic else None,
            "needs_emotional_response": jev_semantic["needs_emotional_response"] if jev_semantic else None,
        }
        if jev_result is not None:
            jev_diag.update({
                "decision_type": jev_result.decision_type,
                "schema_version": jev_result.schema_version,
                "decision_id": jev_result.decision_id,
                "trace_id": jev_result.trace_id,
                "input_hash": jev_result.input_hash,
                "mode": jev_result.mode,
                "confidence": jev_result.confidence,
                "cached": jev_result.cached,
                # `api_latency_ms` is the original remote duration when cached;
                # lookup_elapsed_ms below is this turn's actual wait.
                "api_latency_ms": jev_result.latency_ms,
                "answers": {
                    key: {
                        "type": answer.type,
                        "choice": answer.choice,
                        "score": answer.score,
                        "noul": answer.noul,
                        "probabilities": dict(answer.probabilities),
                        "confidence": answer.confidence,
                        "legend": dict(answer.legend),
                    }
                    for key, answer in jev_result.answers.items()
                },
                "usage": dict(jev_result.usage),
            })

        base.emotion_comparison = {
            "schema_version": "semantic_vad_comparison_v2",
            "selected_source": selected_source,
            "selected_affect": {
                "user_emotion": base.user_emotion,
                "valence": base.valence,
                "arousal": base.arousal,
                "dominance": base.dominance,
            },
            "score_sets": {
                "vad_model": vad_scores,
                "jev": jev_scores,
                "llm": llm_scores,
                "jev_llm": jev_llm_scores or None,
                "combined": combined_scores,
            },
            "raw_score_sets": {
                "vad_model": (
                    dict(zip(("valence", "arousal", "dominance"), vad_score.raw))
                    if vad_score is not None else None
                ),
                "jev": jev_raw_scores,
                "combined": None,
            },
            "score_scales": {
                "vad_model_raw": "1..5",
                "jev_raw": "0..4",
                "score_sets": "normalized 0..1",
                "selected_valence": "-1..1",
            },
            "combined_status": combined_status,
            "jev_weight": self._jev_weight,
            "numeric_blend_threshold": self.NUMERIC_BLEND_THRESHOLD,
            "numeric_jev_weights": numeric_weights,
            "llm_error_kind": llm_error_kind,
            "vad_model": {
                **vad_attempt,
                "raw_scores": list(vad_score.raw) if vad_score is not None else None,
                "reported_axes": dict(vad_score.reported_axes) if vad_score is not None else None,
            },
            "jev": jev_diag,
            "decision_comparison": (
                {
                    "jev": jev_mapped.prompt_values(),
                    "llm": llm_decision,
                    "jev_raw": jev_diag,
                    "selected_decision_source": "jev" if jev_selected else "llm",
                    "affect_source": selected_source,
                    "numeric_decision_source": "jev_llm_blend" if numeric_weights else "unblended",
                }
                if jev_mapped is not None and llm_decision is not None else None
            ),
            "jev_target_scope": "current_user_message",
            "jev_semantic_attributes": jev_semantic["attributes"] if jev_semantic else None,
            "jev_emotion_label_distribution": jev_semantic["emotion_label_distribution"] if jev_semantic else None,
            "existing_perception_affect": existing_perception_affect,
        }
        return base
