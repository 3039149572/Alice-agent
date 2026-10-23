from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from ..domain.memory import MemoryMetadata


class MemoryMetadataStore(Protocol):
    async def get_many(self, memory_ids: list[str]) -> dict[str, MemoryMetadata]: ...
    async def save(self, metadata: MemoryMetadata) -> None: ...
    async def record_access(self, memory_ids: list[str], accessed_at: float) -> None: ...
    async def record_access_once(self, memory_ids: list[str], accessed_at: float, operation_id: str) -> bool: ...
    async def delete_many(self, memory_ids: list[str]) -> None: ...
    async def pinned_ids(self) -> list[str]: ...
    async def close(self) -> None: ...


class InMemoryMetadataStore:
    def __init__(self) -> None:
        self._items: dict[str, MemoryMetadata] = {}
        self._lock = asyncio.Lock()
        self._access_operations: set[str] = set()

    async def get_many(self, memory_ids: list[str]) -> dict[str, MemoryMetadata]:
        async with self._lock:
            return {
                memory_id: deepcopy(self._items[memory_id])
                for memory_id in memory_ids
                if memory_id in self._items
            }

    async def save(self, metadata: MemoryMetadata) -> None:
        async with self._lock:
            self._items[metadata.memory_id] = deepcopy(metadata)

    async def record_access(self, memory_ids: list[str], accessed_at: float) -> None:
        async with self._lock:
            for memory_id in set(memory_ids):
                metadata = self._items.get(memory_id)
                if metadata:
                    metadata.access_count += 1
                    metadata.last_accessed_at = accessed_at
                    metadata.updated_at = accessed_at

    async def delete_many(self, memory_ids: list[str]) -> None:
        async with self._lock:
            for memory_id in memory_ids:
                self._items.pop(memory_id, None)

    async def record_access_once(self, memory_ids: list[str], accessed_at: float, operation_id: str) -> bool:
        async with self._lock:
            if operation_id in self._access_operations:
                return False
            for memory_id in set(memory_ids):
                if metadata := self._items.get(memory_id):
                    metadata.access_count += 1
                    metadata.last_accessed_at = accessed_at
                    metadata.updated_at = accessed_at
            self._access_operations.add(operation_id)
            return True

    async def pinned_ids(self) -> list[str]:
        async with self._lock:
            return [key for key, value in self._items.items() if value.pinned]

    async def close(self) -> None:
        return None


class SQLiteMemoryMetadataStore:
    """Alice-owned evaluation data. It intentionally stores no memory content."""

    def __init__(self, database_path: Path, busy_timeout: float = 5.0) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            database_path,
            timeout=max(0.1, busy_timeout),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_metadata (
                    memory_id TEXT PRIMARY KEY,
                    importance REAL NOT NULL,
                    emotional_weight REAL NOT NULL,
                    relationship_weight REAL NOT NULL,
                    confidence REAL NOT NULL,
                    access_count INTEGER NOT NULL,
                    last_accessed_at REAL,
                    pinned INTEGER NOT NULL,
                    decay_rate REAL NOT NULL,
                    created_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_metadata_accessed
                    ON memory_metadata(last_accessed_at DESC);
                CREATE TABLE IF NOT EXISTS memory_access_operations (
                    operation_id TEXT PRIMARY KEY
                );
                """
            )
            self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("memory metadata store is closed")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryMetadata:
        return MemoryMetadata(
            memory_id=row["memory_id"],
            importance=row["importance"],
            emotional_weight=row["emotional_weight"],
            relationship_weight=row["relationship_weight"],
            confidence=row["confidence"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            pinned=bool(row["pinned"]),
            decay_rate=row["decay_rate"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_many(self, memory_ids: list[str]) -> dict[str, MemoryMetadata]:
        if not memory_ids:
            return {}
        return await asyncio.to_thread(self._get_many_sync, memory_ids)

    def _get_many_sync(self, memory_ids: list[str]) -> dict[str, MemoryMetadata]:
        unique = list(dict.fromkeys(memory_ids))
        placeholders = ",".join("?" for _ in unique)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"SELECT * FROM memory_metadata WHERE memory_id IN ({placeholders})",
                unique,
            ).fetchall()
            return {row["memory_id"]: self._from_row(row) for row in rows}

    async def save(self, metadata: MemoryMetadata) -> None:
        await asyncio.to_thread(self._save_sync, metadata)

    def _save_sync(self, metadata: MemoryMetadata) -> None:
        now = time.time()
        metadata.updated_at = now
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO memory_metadata (
                    memory_id, importance, emotional_weight, relationship_weight,
                    confidence, access_count, last_accessed_at, pinned,
                    decay_rate, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    importance=excluded.importance,
                    emotional_weight=excluded.emotional_weight,
                    relationship_weight=excluded.relationship_weight,
                    confidence=excluded.confidence,
                    access_count=excluded.access_count,
                    last_accessed_at=excluded.last_accessed_at,
                    pinned=excluded.pinned,
                    decay_rate=excluded.decay_rate,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                (
                    metadata.memory_id,
                    metadata.importance,
                    metadata.emotional_weight,
                    metadata.relationship_weight,
                    metadata.confidence,
                    metadata.access_count,
                    metadata.last_accessed_at,
                    int(metadata.pinned),
                    metadata.decay_rate,
                    metadata.created_at,
                    now,
                ),
            )
            self._connection.commit()

    async def record_access(self, memory_ids: list[str], accessed_at: float) -> None:
        if memory_ids:
            await asyncio.to_thread(self._record_access_sync, memory_ids, accessed_at)

    def _record_access_sync(self, memory_ids: list[str], accessed_at: float) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.executemany(
                """
                UPDATE memory_metadata
                SET access_count = access_count + 1,
                    last_accessed_at = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                [(accessed_at, accessed_at, memory_id) for memory_id in set(memory_ids)],
            )
            self._connection.commit()

    async def delete_many(self, memory_ids: list[str]) -> None:
        def remove():
            with self._lock, self._connection:
                self._ensure_open()
                self._connection.executemany("DELETE FROM memory_metadata WHERE memory_id=?", [(key,) for key in set(memory_ids)])
        await asyncio.to_thread(remove)

    async def record_access_once(self, memory_ids: list[str], accessed_at: float, operation_id: str) -> bool:
        def apply():
            with self._lock, self._connection:
                self._ensure_open()
                self._connection.execute("BEGIN IMMEDIATE")
                if self._connection.execute("SELECT 1 FROM memory_access_operations WHERE operation_id=?", (operation_id,)).fetchone():
                    return False
                self._connection.executemany(
                    "UPDATE memory_metadata SET access_count=access_count+1,last_accessed_at=?,updated_at=? WHERE memory_id=?",
                    [(accessed_at, accessed_at, key) for key in set(memory_ids)],
                )
                self._connection.execute("INSERT INTO memory_access_operations VALUES (?)", (operation_id,))
                return True
        return await asyncio.to_thread(apply)

    async def pinned_ids(self) -> list[str]:
        def query():
            with self._lock:
                self._ensure_open()
                return [row[0] for row in self._connection.execute("SELECT memory_id FROM memory_metadata WHERE pinned=1")]
        return await asyncio.to_thread(query)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
