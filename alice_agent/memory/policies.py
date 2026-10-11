from __future__ import annotations

from typing import Protocol

from ..domain.memory import MemoryItem


class MemoryDecayPolicy(Protocol):
    def calculate(self, item: MemoryItem, now: float) -> float: ...


class NoDecayPolicy:
    """v0.4 placeholder: metadata is not destructively decayed."""

    def calculate(self, item: MemoryItem, now: float) -> float:
        return 1.0
