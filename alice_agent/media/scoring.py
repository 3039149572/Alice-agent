from __future__ import annotations

import math
import re

from ..domain.action import ActionPlan
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from .models import MediaCandidate, RankedMediaCandidate


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _grams(text: str) -> set[str]:
    compact = re.sub(r"[^\w\u3400-\u9fff]+", "", text.lower())
    if not compact:
        return set()
    if len(compact) == 1:
        return {compact}
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def lexical_similarity(left: str, right: str) -> float:
    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def vad_similarity(target: tuple[float, float, float], candidate: tuple[float, float, float]) -> float:
    distance = math.sqrt(sum((left - right) ** 2 for left, right in zip(target, candidate)))
    return 1.0 / (1.0 + distance)


def media_target(
    perception: PerceptionResult,
    state: AgentState,
    plan: ActionPlan,
) -> tuple[tuple[float, float, float], float]:
    valence = _clamp01((float(perception.valence) + float(state.mood_valence) + 2.0) / 4.0)
    arousal = _clamp01((float(perception.arousal) + float(state.mood_arousal) + float(plan.energy)) / 3.0)
    dominance = _clamp01(float(plan.directness))
    energy = _clamp01((float(state.energy) + float(plan.energy) + arousal) / 3.0)
    return (valence, arousal, dominance), energy


def rank_candidates(
    candidates: list[MediaCandidate],
    *,
    context_text: str,
    target_vad: tuple[float, float, float],
    target_energy: float,
    kind: str,
    limit: int,
) -> list[RankedMediaCandidate]:
    weights = (0.55, 0.25, 0.20) if kind == "audio" else (0.35, 0.45, 0.20)
    ranked: list[RankedMediaCandidate] = []
    for candidate in candidates:
        values: list[tuple[float, float]] = []
        if candidate.vad is not None:
            values.append((weights[0], vad_similarity(target_vad, candidate.vad)))
        if candidate.description:
            values.append((weights[1], lexical_similarity(context_text, candidate.description)))
        if candidate.energy is not None:
            values.append((weights[2], 1.0 - abs(target_energy - candidate.energy)))
        denominator = sum(weight for weight, _ in values)
        score = sum(weight * value for weight, value in values) / denominator if denominator else 0.0
        ranked.append(RankedMediaCandidate(candidate, _clamp01(score)))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:max(1, int(limit))]
