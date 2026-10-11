from __future__ import annotations

import math
from typing import Any

import httpx

from ...alice_agent.perception.vad import VADModelScore


class VADResponseError(ValueError):
    """The local VAD service returned an invalid or incomplete score vector."""


class HTTPVADModelAdapter:
    """HTTP adapter for the local `/vad` service used by deeplx_genie_tts."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(max(0.1, float(timeout_seconds)))
        )

    @staticmethod
    def _finite_number(value: Any, field: str) -> float:
        if isinstance(value, bool):
            raise VADResponseError(f"{field} must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise VADResponseError(f"{field} must be numeric") from exc
        if not math.isfinite(number):
            raise VADResponseError(f"{field} must be finite")
        return number

    async def score(self, text: str) -> VADModelScore:
        response = await self._client.post(
            f"{self._base_url}/vad", json={"text": text}
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise VADResponseError("VAD service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise VADResponseError("VAD response must be an object")
        values = payload.get("vad")
        if not isinstance(values, (list, tuple)) or len(values) != 3:
            raise VADResponseError("VAD response must contain three axes")
        raw = tuple(self._finite_number(value, "vad") for value in values)
        if any(value < 1.0 or value > 5.0 for value in raw):
            raise VADResponseError("VAD axes must be in the model's 1..5 range")
        normalized = tuple((value - 1.0) / 4.0 for value in raw)
        reported_axes: dict[str, float] = {}
        for name in ("valence", "arousal", "dominance"):
            if name in payload:
                reported_axes[name] = self._finite_number(payload[name], name)
        return VADModelScore(raw=raw, normalized=normalized, reported_axes=reported_axes)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
