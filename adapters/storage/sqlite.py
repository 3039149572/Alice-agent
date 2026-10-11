from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class SQLiteStorageAdapter:
    """Durable small JSON values for Alice state and daily event records."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS alice_values ("
                "namespace TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(namespace, key))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0)

    async def get(self, namespace: str, key: str) -> Any | None:
        def query() -> Any | None:
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT payload FROM alice_values WHERE namespace=? AND key=?",
                    (namespace, key),
                ).fetchone()
                return json.loads(row[0]) if row else None

        async with self._lock:
            return await asyncio.to_thread(query)

    async def set(self, namespace: str, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)

        def query() -> None:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO alice_values(namespace,key,payload) VALUES(?,?,?) "
                    "ON CONFLICT(namespace,key) DO UPDATE SET payload=excluded.payload",
                    (namespace, key, payload),
                )

        async with self._lock:
            await asyncio.to_thread(query)

    async def delete(self, namespace: str, key: str) -> None:
        def query() -> None:
            with closing(self._connect()) as db, db:
                db.execute(
                    "DELETE FROM alice_values WHERE namespace=? AND key=?",
                    (namespace, key),
                )

        async with self._lock:
            await asyncio.to_thread(query)

    async def close(self) -> None:
        return None
