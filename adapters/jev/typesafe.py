from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from ...alice_agent.decision.fallback import (
    DecisionAPIError,
    DecisionTimeoutError,
    DecisionValidationError,
)
from ...alice_agent.decision.models import (
    DecisionAnswer,
    DecisionQuestion,
    DecisionResult,
)


DEFAULT_SYSTEMONE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class TypesafeJevAdapter:
    """Persistent raw HTTP adapter for TypeSafe's documented System One API."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str = "jev-latest",
        timeout_ms: int = 2500,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Jev API key is required when Jev is enabled")
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(max(0.1, timeout_ms / 1000.0)),
        )

    @staticmethod
    def _answer(raw: Any) -> DecisionAnswer:
        if not isinstance(raw, dict):
            raise DecisionValidationError("answer must be an object")
        answer_type = raw.get("type")
        if answer_type not in {"choice", "score", "noul"}:
            raise DecisionValidationError("unknown answer type")
        probabilities = raw.get("probabilities", {})
        legend = raw.get("legend", {})
        if not isinstance(probabilities, dict) or not isinstance(legend, dict):
            raise DecisionValidationError("invalid answer maps")
        return DecisionAnswer(
            type=str(answer_type),
            choice=str(raw["choice"]) if raw.get("choice") is not None else None,
            score=float(raw["score"]) if raw.get("score") is not None else None,
            noul=float(raw["noul"]) if raw.get("noul") is not None else None,
            probabilities={str(key): float(value) for key, value in probabilities.items()},
            confidence=float(raw["confidence"]) if raw.get("confidence") is not None else None,
            legend={str(key): str(value) for key, value in legend.items()},
        )

    async def decide(
        self,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, DecisionQuestion],
        *,
        decision_type: str,
        schema_version: str,
        trace_id: str,
    ) -> DecisionResult:
        payload = {
            "state": state,
            "model": self._model,
            "questions": {key: value.to_payload() for key, value in questions.items()},
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._endpoint, json=payload, headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise DecisionTimeoutError("System One request timed out") from exc
        except httpx.HTTPError as exc:
            raise DecisionAPIError("System One connection failed") from exc
        if response.status_code >= 400:
            raise DecisionAPIError(f"System One returned HTTP {response.status_code}")
        try:
            raw = response.json()
        except ValueError as exc:
            raise DecisionValidationError("System One returned invalid JSON") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
            raise DecisionValidationError("System One response shape is invalid")
        usage = raw.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return DecisionResult(
            decision_id=f"jev_{uuid4().hex}",
            trace_id=trace_id,
            decision_type=decision_type,
            schema_version=schema_version,
            input_hash="",
            model=str(raw.get("model", self._model)),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            answers={key: self._answer(value) for key, value in raw["answers"].items()},
            usage={
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            },
        )

    def _models_endpoint(self) -> str:
        parts = urlsplit(self._endpoint)
        path = parts.path
        if path.endswith("/v1/systemone"):
            path = path[: -len("/v1/systemone")] + "/v1/models"
        else:
            path = "/v1/models"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    async def list_models(self) -> list[dict[str, str]]:
        try:
            response = await self._client.get(
                self._models_endpoint(), headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise DecisionTimeoutError("model list request timed out") from exc
        except httpx.HTTPError as exc:
            raise DecisionAPIError("model list connection failed") from exc
        if response.status_code >= 400:
            raise DecisionAPIError(f"model list returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DecisionValidationError("model list returned invalid JSON") from exc
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise DecisionValidationError("model list response shape is invalid")
        return [
            {
                "name": str(item.get("name", "")),
                "description": str(item.get("description", "")),
                "release_date": str(item.get("release_date", "")),
            }
            for item in models if isinstance(item, dict)
        ]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
