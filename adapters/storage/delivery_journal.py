from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class SQLiteDeliveryJournal:
    """Durable intents for local post-delivery writes; never resends messages."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS delivery_commits (
                    id TEXT PRIMARY KEY, user_key TEXT NOT NULL,
                    digest TEXT NOT NULL, payload TEXT, created_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_pending ON delivery_commits(user_key,created_at) WHERE payload IS NOT NULL;
            """)

    async def _run(self, function):
        def run():
            with self._lock, self._db:
                if self._closed:
                    raise RuntimeError("delivery journal is closed")
                return function()
        return await asyncio.to_thread(run)

    async def prepare(self, operation_id, payload):
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        def write():
            row = self._db.execute("SELECT digest,payload FROM delivery_commits WHERE id=?", (operation_id,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("delivery id already has a different transcript")
                return row["payload"] is not None
            self._db.execute("INSERT INTO delivery_commits VALUES (?,?,?,?,?,NULL)",
                             (operation_id, payload["event"]["user_id"], digest, encoded, time.time()))
            return True
        return await self._run(write)

    async def pending(self, user_id=None):
        def query():
            where, args = "payload IS NOT NULL", ()
            if user_id is not None:
                where, args = where + " AND user_key=?", (user_id,)
            return [(row["id"], json.loads(row["payload"])) for row in self._db.execute(
                f"SELECT id,payload FROM delivery_commits WHERE {where} ORDER BY created_at,id", args,
            )]
        return await self._run(query)

    async def complete(self, operation_id):
        def write():
            row = self._db.execute("SELECT user_key FROM delivery_commits WHERE id=? AND payload IS NOT NULL", (operation_id,)).fetchone()
            if row:
                # Remove private transcript as soon as all stores have committed.
                # A compact receipt prevents an old caller replaying numeric deltas.
                user_hash = hashlib.sha256(row[0].encode()).hexdigest()
                self._db.execute("UPDATE delivery_commits SET user_key=?,payload=NULL,completed_at=? WHERE id=?", (user_hash, time.time(), operation_id))
        await self._run(write)

    async def close(self):
        def close():
            with self._lock:
                if not self._closed:
                    self._db.close()
                    self._closed = True
        await asyncio.to_thread(close)
