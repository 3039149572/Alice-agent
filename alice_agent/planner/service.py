from __future__ import annotations

from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.history import ConversationTurn
from ..domain.llm import LLMRequest
from ..domain.memory import MemoryItem
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from ..relationship.models import RelationshipState
from ..llm.service import LLMService
from .parser import ActionPlanParser


class PlannerService:
    def __init__(
        self,
        llm: LLMService,
        context_builder: object,
        parser: ActionPlanParser,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._context_builder = context_builder
        self._parser = parser
        self._enabled = enabled

    async def plan(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        memories: list[MemoryItem],
        history: list[ConversationTurn],
        relationship: RelationshipState | None = None,
    ) -> ActionPlan:
        if not self._enabled:
            return ActionPlan.fallback()
        system_prompt, prompt = self._context_builder.for_planner(
            event, perception, state, memories, history, relationship
        )
        result = await self._llm.generate(
            LLMRequest(
                stage="planner",
                session_id=event.session_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        )
        return self._parser.parse(result.text, {item.id for item in memories})
