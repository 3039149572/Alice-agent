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
            db.execute("CREATE TABLE IF NOT EXISTS alice_value_operations (operation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, key TEXT NOT NULL)")

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

    async def mutate_once(self, namespace: str, key: str, operation_id: str, transform):
        def query():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT payload FROM alice_values WHERE namespace=? AND key=?", (namespace, key)).fetchone()
                value = json.loads(row[0]) if row else None
                done = db.execute("SELECT namespace,key FROM alice_value_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if done:
                    if tuple(done) != (namespace, key):
                        raise ValueError("operation id belongs to a different state")
                    return value
                result = transform(value)
                payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
                db.execute("INSERT INTO alice_values VALUES (?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET payload=excluded.payload", (namespace, key, payload))
                db.execute("INSERT INTO alice_value_operations VALUES (?,?,?)", (operation_id, namespace, key))
                return result
        async with self._lock:
            return await asyncio.to_thread(query)

    async def close(self) -> None:
        return None

    async def delete_prefix(self, namespace: str, prefix: str) -> None:
        def query():
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM alice_values WHERE namespace=? AND substr(key,1,?)=?", (namespace, len(prefix), prefix))
        async with self._lock:
            await asyncio.to_thread(query)

    async def transform_namespace(self, namespace: str, transform) -> None:
        def query():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                for key, encoded in db.execute("SELECT key,payload FROM alice_values WHERE namespace=?", (namespace,)).fetchall():
                    try:
                        original = json.loads(encoded)
                    except (ValueError, TypeError):
                        continue
                    updated = transform(original)
                    if updated is None:
                        db.execute("DELETE FROM alice_values WHERE namespace=? AND key=?", (namespace, key))
                    elif updated != original:
                        db.execute("UPDATE alice_values SET payload=? WHERE namespace=? AND key=?", (json.dumps(updated, ensure_ascii=False, allow_nan=False), namespace, key))
        async with self._lock:
            await asyncio.to_thread(query)
