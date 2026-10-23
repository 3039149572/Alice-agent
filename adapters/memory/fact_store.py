from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from ...alice_agent.domain.memory import MemoryItem
from ...alice_agent.memory.facts import FactClaim


class SQLiteFactVersions:
    """Versioned user statements, committed with their original turn journal."""

    def __init__(self, connection, scope, upsert, get_item):
        self.db, self.scope, self.upsert, self.get_item = connection, scope, upsert, get_item

    def create_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_fact_versions (
                memory_id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
                fact_key TEXT NOT NULL, value TEXT NOT NULL, version INTEGER NOT NULL,
                valid_from REAL NOT NULL, valid_until REAL, status TEXT NOT NULL,
                superseded_by TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_fact_current
                ON memory_fact_versions(scope_kind,scope_id,fact_key) WHERE status='active';
            CREATE INDEX IF NOT EXISTS idx_memory_fact_history
                ON memory_fact_versions(scope_kind,scope_id,fact_key,version);
            CREATE TABLE IF NOT EXISTS memory_fact_sources (
                memory_id TEXT NOT NULL, trace_id TEXT NOT NULL, session_id TEXT NOT NULL,
                evidence TEXT NOT NULL, observed_at REAL NOT NULL,
                PRIMARY KEY(memory_id,trace_id)
            );
        """)

    def apply(self, source: MemoryItem, claims: list[FactClaim]) -> list[MemoryItem]:
        owner = str(source.metadata.get("user_id" if self.scope == "user" else "session_id", ""))
        trace = str(source.metadata.get("trace_id", source.id))
        timestamp = float(source.created_at or 0.0)
        persisted = []
        for claim in claims:
            current = self.db.execute(
                "SELECT * FROM memory_fact_versions WHERE scope_kind=? AND scope_id=? AND fact_key=? AND status='active'",
                (self.scope, owner, claim.key),
            ).fetchone()
            if current is not None and current["value"] == claim.value:
                item = self.get_item(current["memory_id"])
            else:
                identity = json.dumps([self.scope, owner, claim.key, trace, claim.value], ensure_ascii=False)
                memory_id = "fact_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                existing = self.get_item(memory_id)
                if existing is not None:
                    persisted.append(existing)
                    continue
                version = int(self.db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM memory_fact_versions WHERE scope_kind=? AND scope_id=? AND fact_key=?",
                    (self.scope, owner, claim.key),
                ).fetchone()[0])
                newer = current is None or timestamp >= current["valid_from"]
                if current is not None and newer:
                    self.db.execute(
                        "UPDATE memory_fact_versions SET status='superseded',valid_until=?,superseded_by=? WHERE memory_id=?",
                        (timestamp, memory_id, current["memory_id"]),
                    )
                    old = self.get_item(current["memory_id"])
                    old.metadata.update({"fact_status": "superseded", "valid_until": timestamp,
                                         "superseded_by": memory_id})
                    self.upsert(old, require_existing=True)
                status = "active" if newer else "historical"
                metadata = dict(source.metadata)
                metadata.pop("extract_explicit_facts", None)
                metadata.update({"fact_key": claim.key, "fact_value": claim.value, "fact_version": version,
                                 "fact_status": status, "evidence": claim.evidence,
                                 "source_type": source.metadata.get("source_type", "user_statement"), "valid_from": timestamp})
                item = replace(source, id=memory_id, content=claim.evidence,
                               memory_type="user_fact", metadata=metadata)
                self.upsert(item, require_existing=False)
                self.db.execute(
                    "INSERT INTO memory_fact_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, self.scope, owner, claim.key, claim.value, version, timestamp,
                     None if newer else current["valid_from"], status,
                     None if newer else current["memory_id"]),
                )
            if item is None:
                raise RuntimeError("Fact version has no corresponding memory")
            self.db.execute(
                "INSERT OR IGNORE INTO memory_fact_sources VALUES (?,?,?,?,?)",
                (item.id, trace, str(source.metadata.get("session_id", "")), claim.evidence, timestamp),
            )
            persisted.append(item)
        return persisted
