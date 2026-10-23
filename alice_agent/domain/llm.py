from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMRequest:
    stage: str
    session_id: str
    prompt: str
    system_prompt: str
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str | None = None
