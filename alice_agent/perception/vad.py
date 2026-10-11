from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VADModelScore:
    """One VAD model response, preserving its original and normalized axes."""

    raw: tuple[float, float, float]
    normalized: tuple[float, float, float]
    reported_axes: dict[str, float]


class VADModelPort(Protocol):
    async def score(self, text: str) -> VADModelScore: ...

    async def close(self) -> None: ...
