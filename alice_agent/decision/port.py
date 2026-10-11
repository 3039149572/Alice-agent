from __future__ import annotations

from typing import Any, Protocol

from .models import DecisionQuestion, DecisionResult


class DecisionModelPort(Protocol):
    async def decide(
        self,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, DecisionQuestion],
        *,
        decision_type: str,
        schema_version: str,
        trace_id: str,
    ) -> DecisionResult: ...

    async def close(self) -> None: ...
