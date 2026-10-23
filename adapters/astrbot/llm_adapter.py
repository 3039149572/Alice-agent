from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger

from ...alice_agent.domain.llm import LLMRequest, LLMResult


class AstrBotLLMAdapter:
    """Use the session provider and its configured AstrBot fallback providers."""

    def __init__(self, context: Any, *, attempt_timeout: float = 15.0, fallback_reserve: float = 5.0) -> None:
        self._context = context
        self._attempt_timeout = max(0.01, float(attempt_timeout))
        self._fallback_reserve = max(0.0, float(fallback_reserve))
        self._attempts = deque(maxlen=256)

    def attempt_snapshot(self) -> list[dict[str, Any]]:
        """Bounded provider timings, without prompts, response text or user IDs."""
        return [dict(item) for item in self._attempts]

    async def generate(self, request: LLMRequest) -> LLMResult:
        timeout = request.timeout_seconds if request.timeout_seconds is not None else 30.0
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError("Alice provider budget exhausted")
        deadline = time.monotonic() + timeout
        provider_id = await self._context.get_current_chat_provider_id(request.session_id)
        candidates = [provider_id, *self._fallback_ids(request.session_id, provider_id)]
        # Context.llm_generate calls one provider only; fallback normally lives in
        # AstrBot's agent runner, which Alice's framework-independent Core bypasses.
        for index, candidate_id in enumerate(candidates):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Alice provider budget exhausted")
            reserve = min(self._fallback_reserve, remaining / 2) if index + 1 < len(candidates) else 0.0
            attempt_budget = remaining if len(candidates) == 1 else min(remaining, self._attempt_timeout, remaining - reserve)
            started = time.perf_counter()
            status, error_kind = "ok", None
            try:
                response = await asyncio.wait_for(self._context.llm_generate(
                    chat_provider_id=candidate_id,
                    prompt=request.prompt,
                    system_prompt=request.system_prompt,
                ), timeout=attempt_budget)
                if getattr(response, "role", None) == "err":
                    raise RuntimeError("AstrBot provider returned an error response")
                text = self._response_text(response)
                if not text.strip():
                    raise RuntimeError("AstrBot provider returned empty text")
                return LLMResult(text=text, model=candidate_id)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status = "timeout" if isinstance(exc, TimeoutError) else "error"
                error_kind = type(exc).__name__
                # Cancellation (including LLMService's total timeout) must escape
                # immediately; never start more calls for an abandoned generation.
                if index + 1 == len(candidates):
                    raise
                logger.warning(
                    "Alice LLM provider %s failed (%s); trying fallback provider %s",
                    candidate_id, type(exc).__name__, candidates[index + 1],
                )
            finally:
                self._attempts.append({
                    "date_utc": datetime.now(timezone.utc).date().isoformat(),
                    "stage": request.stage, "provider": candidate_id, "status": status,
                    "error_kind": error_kind, "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "budget_seconds": round(attempt_budget, 4),
                })

    def _fallback_ids(self, session_id: str, primary_id: str) -> list[str]:
        get_config = getattr(self._context, "get_config", None)
        if get_config is None:
            return []
        try:
            value = get_config(umo=session_id)
            for key in ("agent_runner", "config", "model", "fallback_provider_ids"):
                if not isinstance(value, Mapping):
                    return []
                value = value.get(key, {})
        except Exception as exc:
            logger.warning("Alice LLM fallback config unavailable (%s)", type(exc).__name__)
            return []
        if not isinstance(value, list):
            return []
        seen = {primary_id}
        result: list[str] = []
        for candidate_id in value:
            if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            result.append(candidate_id)
        return result

    @staticmethod
    def _response_text(response: Any) -> str:
        text = str(getattr(response, "completion_text", "") or "")
        if not text.strip():
            chain = getattr(getattr(response, "result_chain", None), "chain", None) or []
            text = "".join(
                str(getattr(component, "text", ""))
                for component in chain
                if getattr(component, "text", None)
            )
        return text

    async def close(self) -> None:
        # AstrBot owns provider lifecycle.
        return None
