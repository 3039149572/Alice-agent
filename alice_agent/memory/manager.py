from __future__ import annotations

import time

from ..domain.action import ActionPlan
from ..domain.event import AgentEvent
from ..domain.memory import MemoryItem, MemoryMetadata
from ..domain.perception import PerceptionResult
from ..domain.response import AgentResponse
from ..domain.state import AgentState
from ..ports.logging import LoggerPort, NullLogger
from ..ports.memory import MemoryPort
from ..relationship.models import RelationshipState
from .importance import MemoryImportanceEvaluator
from .metadata_store import InMemoryMetadataStore, MemoryMetadataStore
from .reranker import MemoryReranker
from .retriever import MemoryRetriever
from .writer import MemoryWriter


class MemoryManager:
    def __init__(
        self,
        port: MemoryPort,
        retriever: MemoryRetriever,
        writer: MemoryWriter,
        reranker: MemoryReranker,
        recall_limit: int,
        enabled: bool = True,
        *,
        metadata_store: MemoryMetadataStore | None = None,
        metadata_enabled: bool = True,
        selected_limit: int = 3,
        importance_evaluator: MemoryImportanceEvaluator | None = None,
        logger: LoggerPort | None = None,
        debug: bool = False,
        decision_gate: object | None = None,
    ) -> None:
        self._port = port
        self._retriever = retriever
        self._writer = writer
        self._reranker = reranker
        self._recall_limit = max(0, recall_limit)
        self._selected_limit = max(0, selected_limit)
        self._enabled = enabled
        self._metadata_enabled = metadata_enabled
        self._metadata = metadata_store or InMemoryMetadataStore()
        self._importance = importance_evaluator or MemoryImportanceEvaluator()
        self._logger = logger or NullLogger()
        self._debug = debug
        self._decision_gate = decision_gate

    @staticmethod
    def _apply_metadata(item: MemoryItem, metadata: MemoryMetadata) -> None:
        item.importance = metadata.importance
        item.emotional_weight = metadata.emotional_weight
        item.relationship_weight = metadata.relationship_weight
        item.confidence = metadata.confidence
        item.access_count = metadata.access_count
        item.last_accessed_at = metadata.last_accessed_at
        item.pinned = metadata.pinned

    async def _merge_metadata(
        self,
        items: list[MemoryItem],
        perception: PerceptionResult,
        state: AgentState,
        relationship: RelationshipState,
    ) -> None:
        stored: dict[str, MemoryMetadata] = {}
        if self._metadata_enabled:
            try:
                stored = await self._metadata.get_many([item.id for item in items])
            except Exception:
                self._logger.exception("Memory metadata read failed; using conservative defaults")
        for item in items:
            metadata = stored.get(item.id)
            if metadata is None:
                evaluated = self._importance.evaluate(
                    perception, state, item, relationship, new_observation=False
                )
                metadata = MemoryMetadata(
                    memory_id=item.id,
                    importance=evaluated.importance,
                    emotional_weight=evaluated.emotional_weight,
                    relationship_weight=evaluated.relationship_weight,
                    confidence=item.confidence,
                    created_at=item.created_at,
                    updated_at=time.time(),
                )
                if self._metadata_enabled:
                    try:
                        await self._metadata.save(metadata)
                    except Exception:
                        self._logger.exception(
                            f"Memory metadata initialize failed id={item.id}"
                        )
            self._apply_metadata(item, metadata)

    async def recall(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        relationship: RelationshipState | None = None,
    ) -> list[MemoryItem]:
        if not self._enabled:
            return []
        relationship = relationship or RelationshipState(user_id=event.user_id)
        query, candidates = await self._retriever.recall(event, perception, state)
        await self._merge_metadata(candidates, perception, state, relationship)
        if self._decision_gate is not None:
            candidates = await self._decision_gate.evaluate(event, perception, candidates)
        result = self._reranker.rerank(
            candidates, self._recall_limit, query=query
        )
        self._logger.info(
            f"[{event.trace_id}] MEMORY CANDIDATES count={len(candidates)} "
            f"selected={len(result.selected)}"
        )
        if self._debug:
            by_id = {item.id: item for item in candidates}
            for score in result.scores:
                item = by_id.get(score.memory_id)
                summary = (item.content[:80] + "…") if item and len(item.content) > 80 else (item.content if item else "")
                self._logger.debug(
                    f"[{event.trace_id}] MEMORY SCORE id={score.memory_id} "
                    f"retrieval={score.retrieval:.3f} importance={score.importance:.3f} "
                    f"emotion={score.emotion:.3f} relationship={score.relationship:.3f} "
                    f"recency={score.recency:.3f} final={score.final_score:.3f} "
                    f"jev={item.metadata.get('jev_relevance') if item else None} "
                    f"accepted={score.accepted} reason={score.reason or '-'} "
                    f"summary={summary!r}"
                )
        return result.selected

    async def observe(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
        state: AgentState,
        plan: ActionPlan,
        response: AgentResponse,
        relationship: RelationshipState | None = None,
        *, strict: bool = False,
    ) -> bool:
        if not self._enabled:
            return False
        relationship = relationship or RelationshipState(user_id=event.user_id)
        observation = self._writer.build_observation(
            event, perception, state, plan, response, relationship
        )
        if observation is None:
            return False
        accepted = await self._port.observe(observation)
        if accepted and observation.persist_candidate and self._metadata_enabled:
            for item in observation.persisted_items or [observation.item]:
                try:
                    # A repeated fact reuses its existing ID. Preserve explicit
                    # pinning/access metadata rather than resetting it per turn.
                    if item.id in await self._metadata.get_many([item.id]):
                        continue
                    await self._metadata.save(MemoryMetadata(
                        memory_id=item.id,
                        importance=item.importance,
                        emotional_weight=item.emotional_weight,
                        relationship_weight=item.relationship_weight,
                        confidence=item.confidence,
                        created_at=item.created_at,
                        updated_at=time.time(),
                    ))
                except Exception:
                    self._logger.exception(f"Memory metadata write failed id={item.id}")
                    if strict:
                        raise
        return accepted

    def select_for_response(
        self, memories: list[MemoryItem], memory_ids: list[str]
    ) -> list[MemoryItem]:
        requested = set(memory_ids[: self._selected_limit])
        return [item for item in memories if item.id in requested][
            : self._selected_limit
        ]

    async def load_for_delivery(self, event: AgentEvent, memory_ids: list[str]) -> list[MemoryItem]:
        result = []
        for memory_id in dict.fromkeys(memory_ids):
            item = await self._port.get(memory_id)
            if item is not None and item.metadata.get("user_id") == event.user_id:
                result.append(item)
        return result

    async def record_access(self, memories: list[MemoryItem], *, operation_id: str | None = None) -> None:
        if not memories or not self._metadata_enabled:
            return
        accessed_at = time.time()
        if operation_id is None:
            await self._metadata.record_access([item.id for item in memories], accessed_at)
        elif not await self._metadata.record_access_once([item.id for item in memories], accessed_at, operation_id):
            return
        for item in memories:
            item.access_count += 1
            item.last_accessed_at = accessed_at

    async def close(self) -> None:
        await self._port.close()
        await self._metadata.close()
