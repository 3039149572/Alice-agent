from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path

from .models import RelationshipState, SharedExperience


class InMemoryRelationshipStore:
    def __init__(self) -> None:
        self._states: dict[str, RelationshipState] = {}
        self._experiences: dict[str, SharedExperience] = {}
        self._lock = asyncio.Lock()
        self._operations: dict[str, str] = {}

    async def get(self, user_id: str) -> RelationshipState | None:
        async with self._lock:
            state = self._states.get(user_id)
            return deepcopy(state) if state else None

    async def save(self, state: RelationshipState) -> None:
        async with self._lock:
            self._states[state.user_id] = deepcopy(state)

    async def add_shared_experience(self, experience: SharedExperience) -> None:
        async with self._lock:
            self._experiences[experience.id] = deepcopy(experience)

    async def apply_once(self, user_id: str, operation_id: str, transform) -> RelationshipState:
        async with self._lock:
            done = self._operations.get(operation_id)
            if done is not None and done != user_id:
                raise ValueError("relationship operation belongs to another user")
            if done is None:
                old = deepcopy(self._states.get(user_id)) or RelationshipState(user_id=user_id)
                self._states[user_id] = deepcopy(transform(old))
                self._operations[operation_id] = user_id
            return deepcopy(self._states[user_id])

    async def close(self) -> None:
        return None


class SQLiteRelationshipStore:
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
                CREATE TABLE IF NOT EXISTS relationship_states (
                    user_id TEXT PRIMARY KEY,
                    familiarity REAL NOT NULL,
                    trust REAL NOT NULL,
                    closeness REAL NOT NULL,
                    interaction_count INTEGER NOT NULL,
                    first_interaction_at REAL,
                    last_interaction_at REAL,
                    updated_at REAL NOT NULL DEFAULT (unixepoch())
                );
                CREATE TABLE IF NOT EXISTS shared_experiences (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    importance REAL NOT NULL,
                    emotional_weight REAL NOT NULL,
                    relationship_weight REAL NOT NULL,
                    related_memory_ids_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shared_experiences_user_time
                    ON shared_experiences(user_id, timestamp DESC);
                CREATE TABLE IF NOT EXISTS relationship_operations (
                    operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL
                );
                """
            )
            self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("relationship store is closed")

    async def get(self, user_id: str) -> RelationshipState | None:
        return await asyncio.to_thread(self._get_sync, user_id)

    def _get_sync(self, user_id: str) -> RelationshipState | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM relationship_states WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return None
            return RelationshipState(
                user_id=row["user_id"],
                familiarity=row["familiarity"],
                trust=row["trust"],
                closeness=row["closeness"],
                interaction_count=row["interaction_count"],
                first_interaction_at=row["first_interaction_at"],
                last_interaction_at=row["last_interaction_at"],
            )

    async def save(self, state: RelationshipState) -> None:
        await asyncio.to_thread(self._save_sync, state)

    def _save_sync(self, state: RelationshipState) -> None:
        with self._lock, self._connection:
            self._ensure_open()
            self._save_row(state)

    def _save_row(self, state: RelationshipState) -> None:
        self._connection.execute(
                """
                INSERT INTO relationship_states (
                    user_id, familiarity, trust, closeness, interaction_count,
                    first_interaction_at, last_interaction_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, unixepoch())
                ON CONFLICT(user_id) DO UPDATE SET
                    familiarity=excluded.familiarity,
                    trust=excluded.trust,
                    closeness=excluded.closeness,
                    interaction_count=excluded.interaction_count,
                    first_interaction_at=excluded.first_interaction_at,
                    last_interaction_at=excluded.last_interaction_at,
                    updated_at=excluded.updated_at
                """,
                (
                    state.user_id,
                    state.familiarity,
                    state.trust,
                    state.closeness,
                    state.interaction_count,
                    state.first_interaction_at,
                    state.last_interaction_at,
                ),
            )

    async def apply_once(self, user_id: str, operation_id: str, transform) -> RelationshipState:
        def apply():
            with self._lock, self._connection:
                self._ensure_open()
                self._connection.execute("BEGIN IMMEDIATE")
                done = self._connection.execute("SELECT user_id FROM relationship_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if done:
                    if done[0] != user_id:
                        raise ValueError("relationship operation belongs to another user")
                    return self._get_sync(user_id) or RelationshipState(user_id=user_id)
                old = self._get_sync(user_id) or RelationshipState(user_id=user_id)
                state = transform(old)
                self._save_row(state)
                self._connection.execute("INSERT INTO relationship_operations VALUES (?,?)", (operation_id, user_id))
                return state
        return await asyncio.to_thread(apply)

    async def add_shared_experience(self, experience: SharedExperience) -> None:
        await asyncio.to_thread(self._add_shared_experience_sync, experience)

    def _add_shared_experience_sync(self, experience: SharedExperience) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO shared_experiences (
                    id, user_id, title, summary, timestamp, importance,
                    emotional_weight, relationship_weight,
                    related_memory_ids_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    importance=excluded.importance,
                    emotional_weight=excluded.emotional_weight,
                    relationship_weight=excluded.relationship_weight,
                    related_memory_ids_json=excluded.related_memory_ids_json,
                    metadata_json=excluded.metadata_json
                """,
                (
                    experience.id,
                    experience.user_id,
                    experience.title,
                    experience.summary,
                    experience.timestamp,
                    experience.importance,
                    experience.emotional_weight,
                    experience.relationship_weight,
                    json.dumps(experience.related_memory_ids, ensure_ascii=False),
                    json.dumps(experience.metadata, ensure_ascii=False, default=str),
                ),
            )
            self._connection.commit()

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
