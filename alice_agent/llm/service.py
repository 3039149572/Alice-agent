from __future__ import annotations

import asyncio
from dataclasses import replace

from ..domain.llm import LLMRequest, LLMResult
from ..ports.llm import LLMPort
from ..runtime import current_budget, measured


class LLMService:
    def __init__(self, port: LLMPort, timeout: float) -> None:
        self._port = port
        self._timeout = max(1.0, timeout)

    async def generate(self, request: LLMRequest) -> LLMResult:
        budget = current_budget()
        timeout = min(self._timeout, budget.remaining()) if budget else self._timeout
        if timeout <= 0:
            raise TimeoutError("Alice turn budget exhausted")
        bounded = replace(request, timeout_seconds=timeout)
        return await measured(
            request.stage + "_llm",
            asyncio.wait_for(self._port.generate(bounded), timeout=timeout),
        )

    async def close(self) -> None:
        await self._port.close()
