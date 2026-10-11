from __future__ import annotations

from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...
