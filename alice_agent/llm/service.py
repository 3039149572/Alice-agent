from __future__ import annotations

import asyncio

from ..domain.llm import LLMRequest, LLMResult
from ..ports.llm import LLMPort


class LLMService:
    def __init__(self, port: LLMPort, timeout: float) -> None:
        self._port = port
        self._timeout = max(1.0, timeout)

    async def generate(self, request: LLMRequest) -> LLMResult:
        return await asyncio.wait_for(self._port.generate(request), timeout=self._timeout)

    async def close(self) -> None:
        await self._port.close()
