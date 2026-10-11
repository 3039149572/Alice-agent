from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from collections import OrderedDict
from typing import Any, MutableMapping
from uuid import uuid4

from ..ports.logging import LoggerPort, NullLogger
from ..ports.storage import StoragePort
from .fallback import DecisionTimeoutError, DecisionValidationError
from .metrics import DecisionMetrics
from .models import DecisionMode, DecisionQuestion, DecisionResult
from .policies import DecisionEscalationPolicy
from .port import DecisionModelPort


class DecisionService:
    def __init__(
        self,
        port: DecisionModelPort | None,
        *,
        mode: DecisionMode = DecisionMode.OFF,
        model: str = "jev-latest",
        policy: DecisionEscalationPolicy | None = None,
        logger: LoggerPort | None = None,
        cache_ttl_seconds: float = 30.0,
        cache_limit: int = 256,
        metrics_storage: StoragePort | None = None,
        metrics_flush_seconds: float = 30.0,
    ) -> None:
        self._port = port
        self.mode = mode
        self.model = model
        self.policy = policy or DecisionEscalationPolicy()
        self.metrics = DecisionMetrics()
        self._logger = logger or NullLogger()
        self._cache_ttl = max(0.0, cache_ttl_seconds)
        self._cache_limit = max(0, cache_limit)
        self._cache: OrderedDict[str, tuple[float, DecisionResult]] = OrderedDict()
        self._metrics_storage = metrics_storage
        self._metrics_flush_seconds = max(1.0, metrics_flush_seconds)
        self._metrics_lock = asyncio.Lock()
        self._metrics_loaded = False
        self._metrics_writable = True
        self._metrics_task: asyncio.Task | None = None
        self._metrics_stop = asyncio.Event()
        self._closed = False

    async def start(self) -> None:
        if self._metrics_storage is None or self._metrics_loaded or self._closed:
            return
        async with self._metrics_lock:
            if self._metrics_loaded or self._closed:
                return
            try:
                payload = await self._metrics_storage.get("decision_metrics", "totals")
                self.metrics.restore(payload)
            except Exception as exc:
                # A failed read must not overwrite counters we could not load.
                self._metrics_writable = False
                self._logger.warning(f"Alice decision metrics restore failed ({type(exc).__name__})")
            self._metrics_loaded = True
            if self._metrics_writable:
                self._metrics_task = asyncio.create_task(self._metrics_worker())

    async def flush_metrics(self) -> None:
        await self.start()
        async with self._metrics_lock:
            if self._metrics_storage is None or not self._metrics_writable:
                return
            try:
                await self._metrics_storage.set("decision_metrics", "totals", {
                    "schema_version": 1,
                    "buckets": self.metrics.snapshot(),
                })
            except Exception as exc:
                self._logger.warning(f"Alice decision metrics save failed ({type(exc).__name__})")

    async def _metrics_worker(self) -> None:
        while not self._metrics_stop.is_set():
            try:
                await asyncio.wait_for(self._metrics_stop.wait(), self._metrics_flush_seconds)
            except TimeoutError:
                await self.flush_metrics()

    @property
    def enabled(self) -> bool:
        return self.mode != DecisionMode.OFF and self._port is not None

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def _key(
        self,
        state: Any,
        questions: dict[str, DecisionQuestion],
        decision_type: str,
        schema_version: str,
    ) -> tuple[str, str]:
        state_json = self._canonical(state)
        input_hash = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        schema_payload = {key: question.to_payload() for key, question in questions.items()}
        raw = self._canonical({
            "model": self.model,
            "decision_type": decision_type,
            "schema_version": schema_version,
            "state_hash": input_hash,
            "questions": schema_payload,
        })
        return hashlib.sha256(raw.encode("utf-8")).hexdigest(), input_hash

    @staticmethod
    def _validate(result: DecisionResult, questions: dict[str, DecisionQuestion]) -> None:
        if set(result.answers) != set(questions):
            raise DecisionValidationError("partial or unexpected decision answers")
        for key, question in questions.items():
            answer = result.answers[key]
            if answer.type != question.type:
                raise DecisionValidationError("answer type does not match question")
            if question.type == "choice":
                criteria = question.criteria if isinstance(question.criteria, dict) else {}
                if answer.choice not in criteria or set(answer.probabilities) != set(criteria):
                    raise DecisionValidationError("invalid choice answer")
            elif question.type == "score":
                levels = question.criteria if isinstance(question.criteria, list) else []
                if (
                    answer.score is None
                    or not math.isfinite(float(answer.score))
                    or answer.score < 0
                    or answer.score > len(levels) - 1
                ):
                    raise DecisionValidationError("invalid score answer")
            elif answer.noul is None:
                raise DecisionValidationError("invalid noul answer")

    @staticmethod
    def _confidence(result: DecisionResult) -> float:
        if not result.answers:
            return 0.0
        return sum(answer.effective_confidence for answer in result.answers.values()) / len(result.answers)

    async def decide(
        self,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, DecisionQuestion],
        *,
        decision_type: str,
        schema_version: str,
        trace_id: str,
        attempt_diagnostics: MutableMapping[str, Any] | None = None,
    ) -> DecisionResult | None:
        await self.start()
        attempt_started = time.perf_counter()
        if not self.enabled:
            if attempt_diagnostics is not None:
                attempt_diagnostics.update({
                    "status": "disabled",
                    "cached": False,
                    "api_latency_ms": None,
                    "lookup_elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
                    "error_kind": None,
                })
            return None
        cache_key, input_hash = self._key(state, questions, decision_type, schema_version)
        now = time.monotonic()
        decision_id = f"decision_{uuid4().hex}"
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] <= self._cache_ttl:
            result = copy.deepcopy(cached[1])
            result.decision_id = decision_id
            result.trace_id = trace_id
            result.cached = True
            if attempt_diagnostics is not None:
                attempt_diagnostics.update({
                    "status": "cached",
                    "cached": True,
                    "api_latency_ms": result.latency_ms,
                    "lookup_elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
                    "error_kind": None,
                    "input_hash": input_hash,
                    "model": result.model or self.model,
                    "confidence": result.confidence,
                    "decision_id": decision_id,
                })
            self._logger.debug(
                f"[{trace_id}] JEV TRACE id={decision_id} type={decision_type} "
                f"schema={schema_version} model={self.model} mode={self.mode.value} "
                f"cached=true input_hash={input_hash[:12]}"
            )
            return result
        bucket = self.metrics.bucket(decision_type)
        bucket.calls += 1
        started = time.perf_counter()
        resolved_model = self.model
        try:
            result = await self._port.decide(
                state, questions,
                decision_type=decision_type,
                schema_version=schema_version,
                trace_id=trace_id,
            )
            result.decision_id = decision_id
            resolved_model = result.model or self.model
            self._validate(result, questions)
            result.confidence = self._confidence(result)
            result.input_hash = input_hash
            result.mode = self.mode.value
            if attempt_diagnostics is not None:
                attempt_diagnostics.update({
                    "status": "ok",
                    "cached": False,
                    "api_latency_ms": result.latency_ms,
                    "error_kind": None,
                    "input_hash": input_hash,
                    "model": result.model or self.model,
                    "confidence": result.confidence,
                    "decision_id": decision_id,
                    "usage": dict(result.usage),
                })
            level = self.policy.classify(result.confidence)
            setattr(bucket, f"{level}_confidence", getattr(bucket, f"{level}_confidence") + 1)
            bucket.input_tokens += int(result.usage.get("input_tokens", 0))
            bucket.output_tokens += int(result.usage.get("output_tokens", 0))
            answer_summary = {
                key: {
                    "type": answer.type,
                    "choice": answer.choice,
                    "score": answer.score,
                    "noul": answer.noul,
                    "confidence": answer.confidence,
                    "probabilities": answer.probabilities,
                }
                for key, answer in result.answers.items()
            }
            self._logger.debug(
                f"[{trace_id}] JEV RESULT id={decision_id} type={decision_type} "
                f"confidence={result.confidence:.3f} fallback=false answers={answer_summary!r}"
            )
            if self._cache_limit:
                self._cache[cache_key] = (now, copy.deepcopy(result))
                while len(self._cache) > self._cache_limit:
                    self._cache.popitem(last=False)
            return result
        except DecisionTimeoutError:
            bucket.timeouts += 1
            bucket.fallbacks += 1
            self._logger.warning(
                f"[{trace_id}] JEV decision={decision_type} timeout input_hash={input_hash[:12]}"
            )
            if attempt_diagnostics is not None:
                attempt_diagnostics.update({
                    "status": "error", "cached": False,
                    "api_latency_ms": None, "error_kind": "DecisionTimeoutError",
                    "input_hash": input_hash, "model": resolved_model,
                })
            return None
        except Exception as exc:
            bucket.errors += 1
            bucket.fallbacks += 1
            self._logger.warning(
                f"[{trace_id}] JEV decision={decision_type} fallback "
                f"error={type(exc).__name__} input_hash={input_hash[:12]}"
            )
            if attempt_diagnostics is not None:
                attempt_diagnostics.update({
                    "status": "error", "cached": False,
                    "api_latency_ms": None, "error_kind": type(exc).__name__,
                    "input_hash": input_hash, "model": resolved_model,
                })
            return None
        finally:
            latency = (time.perf_counter() - started) * 1000.0
            if attempt_diagnostics is not None:
                attempt_diagnostics["lookup_elapsed_ms"] = (
                    time.perf_counter() - attempt_started
                ) * 1000.0
            bucket.latency_total_ms += latency
            self._logger.debug(
                f"[{trace_id}] JEV TRACE type={decision_type} schema={schema_version} "
                f"id={decision_id} model={resolved_model} mode={self.mode.value} latency_ms={latency:.1f} "
                f"input_hash={input_hash[:12]}"
            )

    def record_escalation(self, decision_type: str) -> None:
        self.metrics.bucket(decision_type).llm_escalations += 1

    def record_disagreement(self, decision_type: str) -> None:
        self.metrics.bucket(decision_type).existing_disagreements += 1

    def record_comparison(
        self,
        decision_type: str,
        trace_id: str,
        jev_decision: object,
        existing_decision: object,
    ) -> None:
        self.metrics.bucket(decision_type).existing_comparisons += 1
        disagreement = jev_decision != existing_decision
        if disagreement:
            self.record_disagreement(decision_type)
        self._logger.debug(
            f"[{trace_id}] JEV SHADOW type={decision_type} "
            f"jev={jev_decision!r} existing={existing_decision!r} "
            f"disagreement={str(disagreement).lower()}"
        )

    def record_llm_avoided(self, decision_type: str) -> None:
        self.metrics.bucket(decision_type).llm_calls_avoided += 1

    async def close(self) -> None:
        if self._closed:
            return
        await self.start()
        self._closed = True
        self._metrics_stop.set()
        if self._metrics_task is not None:
            # Finish any SQLite write already in flight before the final save.
            await asyncio.gather(self._metrics_task, return_exceptions=True)
        try:
            await self.flush_metrics()
        finally:
            try:
                if self._port is not None:
                    await self._port.close()
            finally:
                if self._metrics_storage is not None:
                    await self._metrics_storage.close()
