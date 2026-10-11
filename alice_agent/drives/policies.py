from __future__ import annotations


def clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def saturating_growth(current: float, amount: float) -> float:
    return clamp(current + max(0.0, amount) * (1.0 - current))
