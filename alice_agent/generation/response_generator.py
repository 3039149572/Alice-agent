from __future__ import annotations

from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.llm import LLMRequest
from ..domain.memory import MemoryItem
from ..domain.perception import PerceptionResult
from ..domain.response import AgentResponse
from ..domain.state import AgentState
from ..relationship.models import RelationshipState
from ..llm.service import LLMService


class ResponseGenerator:
    def __init__(self, llm: LLMService, context_builder: object) -> None:
        self._llm = llm
        self._context_builder = context_builder

    async def generate(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        memories: list[MemoryItem],
        plan: ActionPlan,
        history: list[ConversationTurn],
        relationship: RelationshipState | None = None,
    ) -> AgentResponse:
        if plan.action in {"ignore", "wait"}:
            return AgentResponse(text=None, trace_id=event.trace_id, action=plan.action)
        system_prompt, prompt = self._context_builder.for_response(
            event, perception, state, memories, plan, history, relationship
        )
        result = await self._llm.generate(
            LLMRequest(
                stage="response",
                session_id=event.session_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        )
        text = result.text.strip()
        if not text:
            raise ValueError("response generator returned empty text")
        return AgentResponse(text=text, trace_id=event.trace_id, action=plan.action)
