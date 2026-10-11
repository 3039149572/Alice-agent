from __future__ import annotations

from ..domain.event import AgentEvent
from ..domain.response import AgentResponse
from typing import Awaitable, Callable
from .pipeline import AgentPipeline


class AliceAgent:
    def __init__(
        self,
        pipeline: AgentPipeline,
        llm_service: object | None = None,
        decision_service: object | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._llm_service = llm_service
        self._decision_service = decision_service

    async def handle_event(
        self,
        event: AgentEvent,
        is_current: Callable[[], Awaitable[bool]] | None = None,
    ) -> AgentResponse:
        return await self._pipeline.handle_event(event, is_current=is_current)

    async def commit_delivery(
        self, event: AgentEvent, response: AgentResponse, delivered_text: str
    ) -> None:
        await self._pipeline.commit_delivery(event, response, delivered_text)

    async def record_suppressed(
        self, event: AgentEvent, diagnostics: dict[str, object]
    ) -> None:
        await self._pipeline.record_suppressed(event, diagnostics)

    async def close(self) -> None:
        await self._pipeline.close()
        if self._llm_service is not None:
            await self._llm_service.close()
        if self._decision_service is not None:
            await self._decision_service.close()
