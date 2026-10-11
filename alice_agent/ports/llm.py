from __future__ import annotations

from typing import Protocol

from ..domain.llm import LLMRequest, LLMResult


class LLMPort(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResult: ...

    async def close(self) -> None: ...
