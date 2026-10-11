from __future__ import annotations

from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMRequest, LLMResult


class ScriptedLLM:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def generate(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request.stage)
        value = self.responses[request.stage]
        return LLMResult(text=value, model="mock")

    async def close(self) -> None:
        pass
