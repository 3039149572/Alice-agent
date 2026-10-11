from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ...alice_agent.domain.memory import MemoryItem, MemoryObservation, MemoryQuery


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    latin = set(re.findall(r"[a-z0-9_]+", lowered))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    grams = {cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))}
    if len(cjk) == 1:
        grams.add(cjk)
    return latin | grams


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class LocalSQLiteMemoryConfig:
    database_path: Path
    scope: str = "user"
    capture_all_turns: bool = True
    candidate_limit: int = 300
    min_score: float = 0.12
    busy_timeout: float = 5.0

    def __post_init__(self) -> None:
        path = Path(self.database_path).expanduser()
        scope = self.scope.strip().lower()
        if scope not in {"user", "session"}:
            raise ValueError("local memory scope must be 'user' or 'session'")
        object.__setattr__(self, "database_path", path)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "candidate_limit", max(10, int(self.candidate_limit)))
        object.__setattr__(self, "min_score", min(1.0, max(0.0, float(self.min_score))))
        object.__setattr__(self, "busy_timeout", max(0.1, float(self.busy_timeout)))


class LocalSQLiteMemoryAdapter:
    """Persistent, network-free MemoryPort implementation.

    Long-term candidates and the optional raw turn journal are separate. Recall
    only reads approved long-term candidates, so a low-value turn cannot enter
    an LLM prompt merely because turn journaling is enabled.
    """

    def __init__(self, config: LocalSQLiteMemoryConfig) -> None:
        self._config = config
        self._thread_lock = threading.RLock()
        self._closed = False
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            config.database_path,
            timeout=config.busy_timeout,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._thread_lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                f"PRAGMA busy_timeout={int(config.busy_timeout * 1000)}"
            )
            self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                type TEXT NOT NULL,
                importance REAL NOT NULL,
                emotional_weight REAL NOT NULL,
                relationship_weight REAL NOT NULL,
                confidence REAL NOT NULL,
                created_at REAL NOT NULL,
                last_accessed_at REAL,
                metadata_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_user_created
                ON memories(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memories_session_created
                ON memories(session_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT,
                memory_candidate INTEGER NOT NULL,
                importance REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_turns_user_created
                ON conversation_turns(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_turns_session_created
                ON conversation_turns(session_id, created_at DESC);
            PRAGMA user_version=1;
            """
        )
        self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("local memory database is closed")

    @staticmethod
    def _metadata(item: MemoryItem) -> tuple[str, str, str]:
        metadata = item.metadata
        user_id = str(metadata.get("user_id") or "")
        session_id = str(metadata.get("session_id") or "")
        trace_id = str(metadata.get("trace_id") or item.id)
        return user_id, session_id, trace_id

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return MemoryItem(
            id=str(row["id"]),
            content=str(row["content"]),
            memory_type=str(row["type"]),
            source_layer="LOCAL",
            importance=float(row["importance"]),
            emotional_weight=float(row["emotional_weight"]),
            relationship_weight=float(row["relationship_weight"]),
            confidence=float(row["confidence"]),
            created_at=float(row["created_at"]),
            last_accessed_at=(
                float(row["last_accessed_at"])
                if row["last_accessed_at"] is not None
                else None
            ),
            metadata=metadata,
        )

    async def recall(self, query: MemoryQuery) -> list[MemoryItem]:
        if query.limit <= 0:
            return []
        return await asyncio.to_thread(self._recall_sync, query)

    def _recall_sync(self, query: MemoryQuery) -> list[MemoryItem]:
        with self._thread_lock:
            self._ensure_open()
            column = "user_id" if self._config.scope == "user" else "session_id"
            value = query.user_id if self._config.scope == "user" else query.session_id
            rows = self._connection.execute(
                f"SELECT * FROM memories WHERE {column} = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (value, self._config.candidate_limit),
            ).fetchall()

            query_tokens = _tokens(query.text)
            topic_tokens = _tokens(query.topic)
            scored: list[tuple[float, MemoryItem]] = []
            for row in rows:
                item = self._row_to_item(row)
                lexical = _overlap(query_tokens, _tokens(item.content))
                topic = _overlap(topic_tokens, _tokens(str(item.metadata.get("topic", ""))))
                if lexical <= 0.0 and topic <= 0.0:
                    continue
                relevance = 0.8 * lexical + 0.2 * topic
                if relevance >= self._config.min_score:
                    item.retrieval_score = relevance
                    scored.append((relevance, item))

            scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
            selected = [item for _, item in scored[: query.limit]]
            return selected

    async def observe(self, observation: MemoryObservation) -> bool:
        return await asyncio.to_thread(self._observe_sync, observation)

    def _observe_sync(self, observation: MemoryObservation) -> bool:
        item = observation.item
        user_id, session_id, trace_id = self._metadata(item)
        with self._thread_lock:
            self._ensure_open()
            wrote_turn = False
            if self._config.capture_all_turns:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO conversation_turns (
                        trace_id, user_id, session_id, user_text, assistant_text,
                        memory_candidate, importance, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace_id, user_id, session_id, item.content,
                        observation.assistant_text, int(observation.persist_candidate),
                        item.importance, item.created_at,
                    ),
                )
                wrote_turn = cursor.rowcount > 0
            wrote_memory = False
            if observation.persist_candidate:
                self._upsert_item(item, require_existing=False)
                wrote_memory = True
            self._connection.commit()
            return wrote_turn or wrote_memory

    def _upsert_item(self, item: MemoryItem, *, require_existing: bool) -> None:
        user_id, session_id, _ = self._metadata(item)
        if require_existing:
            exists = self._connection.execute(
                "SELECT 1 FROM memories WHERE id = ?", (item.id,)
            ).fetchone()
            if exists is None:
                raise KeyError(item.id)
        metadata_json = json.dumps(
            item.metadata, ensure_ascii=False, separators=(",", ":"), default=str
        )
        self._connection.execute(
            """
            INSERT INTO memories (
                id, user_id, session_id, content, type, importance,
                emotional_weight, relationship_weight, confidence, created_at,
                last_accessed_at, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_id=excluded.user_id, session_id=excluded.session_id,
                content=excluded.content, type=excluded.type,
                importance=excluded.importance,
                emotional_weight=excluded.emotional_weight,
                relationship_weight=excluded.relationship_weight,
                confidence=excluded.confidence, created_at=excluded.created_at,
                last_accessed_at=excluded.last_accessed_at,
                metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
            """,
            (
                item.id, user_id, session_id, item.content, item.memory_type,
                item.importance, item.emotional_weight, item.relationship_weight,
                item.confidence, item.created_at or time.time(), item.last_accessed_at,
                metadata_json, time.time(),
            ),
        )

    async def update(self, item: MemoryItem) -> None:
        await asyncio.to_thread(self._update_sync, item)

    def _update_sync(self, item: MemoryItem) -> None:
        with self._thread_lock:
            self._ensure_open()
            self._upsert_item(item, require_existing=True)
            self._connection.commit()

    async def get(self, memory_id: str) -> MemoryItem | None:
        return await asyncio.to_thread(self._get_sync, memory_id)

    def _get_sync(self, memory_id: str) -> MemoryItem | None:
        with self._thread_lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return self._row_to_item(row) if row is not None else None

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    async def counts(self) -> tuple[int, int]:
        """Return (long-term memories, journaled turns); intended for diagnostics."""
        return await asyncio.to_thread(self._counts_sync)

    def _counts_sync(self) -> tuple[int, int]:
        with self._thread_lock:
            self._ensure_open()
            memories = self._connection.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
            turns = self._connection.execute(
                "SELECT COUNT(*) FROM conversation_turns"
            ).fetchone()[0]
            return int(memories), int(turns)
