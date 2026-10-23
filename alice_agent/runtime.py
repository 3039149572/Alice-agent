"""Framework-independent computation budgets and redacted stage timing."""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, TypeVar


@dataclass
class TurnBudget:
    deadline: float
    latency_ms: dict[str, float] = field(default_factory=dict)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


_BUDGET: ContextVar[TurnBudget | None] = ContextVar("alice_turn_budget", default=None)


def current_budget() -> TurnBudget | None:
    return _BUDGET.get()


@contextmanager
def turn_budget(deadline: float):
    budget = TurnBudget(deadline)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


T = TypeVar("T")


async def measured(stage: str, operation: Awaitable[T]) -> T:
    started = time.perf_counter()
    try:
        return await operation
    finally:
        budget = current_budget()
        if budget is not None:
            budget.latency_ms[stage] = budget.latency_ms.get(stage, 0.0) + (time.perf_counter() - started) * 1000.0
