from __future__ import annotations

import asyncio
import heapq
import json
import uuid
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ...alice_agent.domain.memory import MemoryItem, MemoryObservation, MemoryQuery
from ...alice_agent.memory.lexical import tokens as _tokens, topic_tokens, overlap as _overlap
from ...alice_agent.memory.facts import extract_explicit_facts, fact_query_keys
from .fact_store import SQLiteFactVersions
from .governance import SQLiteMemoryGovernance


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
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        existing = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if existing and version < 3 and str(self._config.database_path) != ":memory:":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self._config.database_path.with_name(
                self._config.database_path.name + f".pre-v3-{stamp}-{uuid.uuid4().hex[:8]}.bak"
            )
            # SQLite's backup API includes committed WAL pages. Abort migration
            # if this recovery point cannot be written; never overwrite it.
            with closing(sqlite3.connect(backup)) as target:
                self._connection.backup(target)
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
            CREATE TABLE IF NOT EXISTS memory_search_documents (
                memory_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS memory_search_terms (
                memory_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(memory_id, kind, term)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_search_terms_lookup
                ON memory_search_terms(kind, term, memory_id);
            CREATE TEMP TABLE IF NOT EXISTS recall_terms (
                kind TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY(kind, term)
            );
            """
        )
        self._facts = SQLiteFactVersions(
            self._connection, self._config.scope, self._upsert_item, self._get_sync,
        )
        self._facts.create_schema()
        self._data = SQLiteMemoryGovernance(
            self._connection, self._config.scope, self._row_to_item, self._upsert_item, self._facts,
        )
        self._data.create_schema()
        # The document marker includes empty-token records, so startup does not
        # repeatedly backfill them. Index and content updates share a transaction.
        with self._connection:
            rows = self._connection.execute(
                "SELECT m.* FROM memories m LEFT JOIN memory_search_documents d "
                "ON d.memory_id=m.id WHERE d.memory_id IS NULL"
            ).fetchall()
            for row in rows:
                item = self._row_to_item(row)
                self._index_item(item)
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version < 3:
                self._connection.execute("PRAGMA user_version=3")

    def _index_item(self, item: MemoryItem) -> None:
        self._connection.execute("DELETE FROM memory_search_terms WHERE memory_id=?", (item.id,))
        terms = [(item.id, "content", term) for term in sorted(_tokens(item.content))]
        terms.extend((item.id, "topic", term) for term in sorted(topic_tokens(item.metadata.get("topic", ""))))
        fact_key = item.metadata.get("fact_key")
        if isinstance(fact_key, str):
            terms.extend((item.id, "fact", term) for term in sorted({fact_key, fact_key.split("/", 1)[0]}))
        self._connection.executemany(
            "INSERT INTO memory_search_terms(memory_id,kind,term) VALUES (?,?,?)", terms,
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_search_documents(memory_id) VALUES (?)", (item.id,),
        )

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
        metadata.update(user_id=str(row["user_id"]), session_id=str(row["session_id"]))
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
        with self._thread_lock, self._connection:
            self._ensure_open()
            column = "user_id" if self._config.scope == "user" else "session_id"
            value = query.user_id if self._config.scope == "user" else query.session_id
            query_tokens = _tokens(query.text)
            query_topics = topic_tokens(query.topic)
            query_facts = fact_query_keys(query.text)
            self._connection.execute("DELETE FROM recall_terms")
            self._connection.executemany(
                "INSERT INTO recall_terms(kind,term) VALUES (?,?)",
                [("content", term) for term in sorted(query_tokens)]
                + [("topic", term) for term in sorted(query_topics)],
            )
            self._connection.executemany(
                "INSERT INTO recall_terms(kind,term) VALUES (?,?)",
                [("fact", term) for term in sorted(query_facts)],
            )
            # The index selects matches throughout the user's/session's entire
            # history. candidate_limit is a fetch batch, never a recency cutoff.
            cursor = self._connection.execute(
                "SELECT DISTINCT m.* FROM recall_terms q "
                "JOIN memory_search_terms t ON t.kind=q.kind AND t.term=q.term "
                "JOIN memories m ON m.id=t.memory_id "
                "LEFT JOIN memory_fact_versions f ON f.memory_id=m.id "
                f"WHERE m.{column}=? AND (f.status IS NULL OR f.status='active')", (value,),
            )
            best: list[tuple[float, float, str, MemoryItem]] = []
            while rows := cursor.fetchmany(self._config.candidate_limit):
                for row in rows:
                    item = self._row_to_item(row)
                    lexical = _overlap(query_tokens, _tokens(item.content))
                    topic = _overlap(query_topics, topic_tokens(item.metadata.get("topic", "")))
                    key = str(item.metadata.get("fact_key", ""))
                    fact_match = bool(query_facts & {key, key.split("/", 1)[0]})
                    if lexical <= 0.0 and topic <= 0.0 and not fact_match:
                        continue
                    relevance = 0.8 * lexical + 0.2 * topic
                    if fact_match:
                        relevance = max(relevance, 0.8)
                    if relevance >= self._config.min_score:
                        item.retrieval_score = relevance
                        entry = (relevance, float(item.created_at or 0.0), item.id, item)
                        if len(best) < query.limit:
                            heapq.heappush(best, entry)
                        else:
                            heapq.heappushpop(best, entry)
            return [entry[3] for entry in sorted(best, reverse=True)]

    async def observe(self, observation: MemoryObservation) -> bool:
        return await asyncio.to_thread(self._observe_sync, observation)

    def _observe_sync(self, observation: MemoryObservation) -> bool:
        item = observation.item
        user_id, session_id, trace_id = self._metadata(item)
        with self._thread_lock, self._connection:
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
                claims = extract_explicit_facts(item.content) if item.metadata.get("extract_explicit_facts") is True else []
                if claims:
                    observation.persisted_items = self._facts.apply(item, claims)
                else:
                    self._upsert_item(item, require_existing=False)
                    observation.persisted_items = [item]
                wrote_memory = True
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
                item.confidence, item.created_at if item.created_at is not None else time.time(), item.last_accessed_at,
                metadata_json, time.time(),
            ),
        )
        self._index_item(item)

    async def update(self, item: MemoryItem) -> None:
        await asyncio.to_thread(self._update_sync, item)

    def _update_sync(self, item: MemoryItem) -> None:
        with self._thread_lock, self._connection:
            self._ensure_open()
            self._upsert_item(item, require_existing=True)

    async def get(self, memory_id: str) -> MemoryItem | None:
        return await asyncio.to_thread(self._get_sync, memory_id)

    def _get_sync(self, memory_id: str) -> MemoryItem | None:
        with self._thread_lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return self._row_to_item(row) if row is not None else None

    async def manage(self, operation: str, *args, **kwargs):
        if operation not in {"list", "journal", "show", "preview", "delete", "correct", "prune", "jobs", "finish"}:
            raise ValueError("unknown memory data operation")
        return await asyncio.to_thread(self._manage_sync, operation, args, kwargs)

    def _manage_sync(self, operation, args, kwargs):
        with self._thread_lock, self._connection:
            self._ensure_open()
            return getattr(self._data, operation)(*args, **kwargs)

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
