from __future__ import annotations

import asyncio
import json
import re

from ..domain.llm import LLMRequest
from ..domain.memory import MemoryItem
from ..domain.state import AgentState
from ..drives.models import DriveState
from ..llm.service import LLMService
from ..relationship.models import RelationshipState
from .models import CandidateAction, ProactiveActionPlan, UserAvailability


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class ProactivePlanParser:
    def parse(self, text: str) -> ProactiveActionPlan:
        match = _FENCE.match(text or "")
        payload = match.group(1) if match else (text or "")
        try:
            data = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ProactiveActionPlan.do_nothing("planner returned invalid JSON")
        if not isinstance(data, dict) or data.get("action") not in {"do_nothing", "proactive_message"}:
            return ProactiveActionPlan.do_nothing("planner returned unsupported action")
        memory_ids = data.get("memory_ids", [])
        if not isinstance(memory_ids, list):
            memory_ids = []
        return ProactiveActionPlan(
            action=str(data["action"]),
            reason=str(data.get("reason", ""))[:500],
            topic=str(data["topic"])[:200] if data.get("topic") else None,
            response_goal=str(data["response_goal"])[:500] if data.get("response_goal") else None,
            memory_ids=[str(value) for value in memory_ids[:3]],
        )


class ProactivePlanner:
    def __init__(
        self,
        llm: LLMService,
        context_builder: object,
        timeout: float = 20.0,
    ) -> None:
        self._llm = llm
        self._context_builder = context_builder
        self._parser = ProactivePlanParser()
        self._timeout = max(1.0, timeout)

    async def plan(
        self,
        session_id: str,
        candidate: CandidateAction,
        state: AgentState,
        relationship: RelationshipState,
        drives: DriveState,
        memories: list[MemoryItem],
        availability: UserAvailability,
    ) -> ProactiveActionPlan:
        system_prompt, prompt = self._context_builder.for_proactive_planner(
            candidate, state, relationship, drives, memories, availability
        )
        try:
            result = await asyncio.wait_for(
                self._llm.generate(LLMRequest(
                    stage="proactive_planner",
                    session_id=session_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )),
                timeout=self._timeout,
            )
            return self._parser.parse(result.text)
        except Exception:
            return ProactiveActionPlan.do_nothing("proactive planner failed safely")
