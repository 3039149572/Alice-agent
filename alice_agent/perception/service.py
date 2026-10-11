from __future__ import annotations

from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.llm import LLMRequest
from ..domain.perception import PerceptionResult
from ..llm.service import LLMService
from .parser import PerceptionParser


class PerceptionService:
    def __init__(
        self,
        llm: LLMService,
        context_builder: object,
        parser: PerceptionParser,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._context_builder = context_builder
        self._parser = parser
        self._enabled = enabled

    async def analyze(
        self, event: AgentEvent, history: list[ConversationTurn]
    ) -> PerceptionResult:
        if not self._enabled:
            return PerceptionResult.fallback()
        system_prompt, prompt = self._context_builder.for_perception(event, history)
        result = await self._llm.generate(
            LLMRequest(
                stage="perception",
                session_id=event.session_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        )
        return self._parser.parse(result.text)
