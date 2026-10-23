from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, replace

from ...alice_agent.memory.facts import extract_explicit_facts


class SQLiteMemoryGovernance:
    """Runs under the memory adapter's lock and transaction, without model calls."""

    def __init__(self, db, scope_kind, row_to_item, upsert, facts):
        self.db, self.kind = db, scope_kind
        self.item, self.upsert, self.facts = row_to_item, upsert, facts

    def create_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_edits (
                memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                old_content TEXT NOT NULL, new_content TEXT NOT NULL,
                edited_at REAL NOT NULL, trace_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(memory_id,revision)
            );
            CREATE TABLE IF NOT EXISTS memory_cleanup_jobs (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL
            );
        """)

    def _where(self, scope, alias=""):
        prefix = alias + "." if alias else ""
        where, args = f"{prefix}user_id=?", [scope.user_id]
        if self.kind == "session":
            where += f" AND {prefix}session_id=?"
            args.append(scope.session_id)
        return where, args

    def _row(self, scope, memory_id):
        where, args = self._where(scope)
        row = self.db.execute(f"SELECT * FROM memories WHERE {where} AND id=?", (*args, memory_id)).fetchone()
        if row is None:
            raise KeyError("memory not found in your scope")
        return row

    def list(self, scope, page=1, limit=10):
        where, args = self._where(scope, "m")
        limit, page = min(20, max(1, int(limit))), max(1, int(page))
        rows = self.db.execute(
            "SELECT m.*,f.status FROM memories m LEFT JOIN memory_fact_versions f ON f.memory_id=m.id "
            f"WHERE {where} AND (f.status IS NULL OR f.status='active') ORDER BY m.created_at DESC,m.id DESC LIMIT ? OFFSET ?",
            (*args, limit, (page - 1) * limit),
        ).fetchall()
        total = self.db.execute(
            "SELECT COUNT(*) FROM memories m LEFT JOIN memory_fact_versions f ON f.memory_id=m.id "
            f"WHERE {where} AND (f.status IS NULL OR f.status='active')", args,
        ).fetchone()[0]
        return {"items": [asdict(self.item(row)) for row in rows], "total": total, "page": page}

    def show(self, scope, memory_id):
        row = self._row(scope, memory_id)
        fact = self.db.execute("SELECT * FROM memory_fact_versions WHERE memory_id=?", (memory_id,)).fetchone()
        versions = []
        if fact:
            where, args = self._where(scope, "m")
            versions = [dict(x) for x in self.db.execute(
                "SELECT f.* FROM memory_fact_versions f JOIN memories m ON m.id=f.memory_id "
                f"WHERE {where} AND f.scope_kind=? AND f.scope_id=? AND f.fact_key=? ORDER BY f.version DESC LIMIT 20",
                (*args, fact["scope_kind"], fact["scope_id"], fact["fact_key"]),
            )]
        sources = [dict(x) for x in self.db.execute(
            "SELECT trace_id,evidence,observed_at FROM memory_fact_sources WHERE memory_id=? ORDER BY observed_at DESC LIMIT 20",
            (memory_id,),
        )]
        edits = [dict(x) for x in self.db.execute(
            "SELECT revision,old_content,new_content,edited_at FROM memory_edits WHERE memory_id=? ORDER BY revision DESC LIMIT 20",
            (memory_id,),
        )]
        return {"item": asdict(self.item(row)), "versions": versions, "sources": sources, "edits": edits}

    def journal(self, scope, page=1, limit=10):
        where, args = self._where(scope)
        page, limit = max(1, int(page)), min(20, max(1, int(limit)))
        rows = self.db.execute(f"SELECT user_text,assistant_text,created_at FROM conversation_turns WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, (page - 1) * limit))
        total = self.db.execute(f"SELECT COUNT(*) FROM conversation_turns WHERE {where}", args).fetchone()[0]
        return {"items": [dict(row) for row in rows], "total": total, "page": page}

    def _family(self, scope, memory_id):
        row = self._row(scope, memory_id)
        fact = self.db.execute("SELECT * FROM memory_fact_versions WHERE memory_id=?", (memory_id,)).fetchone()
        if not fact:
            return [row]
        where, args = self._where(scope, "m")
        return self.db.execute(
            "SELECT m.* FROM memories m JOIN memory_fact_versions f ON f.memory_id=m.id "
            f"WHERE {where} AND f.scope_kind=? AND f.scope_id=? AND f.fact_key=? ORDER BY m.id",
            (*args, fact["scope_kind"], fact["scope_id"], fact["fact_key"]),
        ).fetchall()

    def preview(self, scope, memory_id=None):
        if memory_id is None:
            where, args = self._where(scope)
            rows = self.db.execute(f"SELECT * FROM memories WHERE {where} ORDER BY id", args).fetchall()
            turns = self.db.execute(f"SELECT trace_id FROM conversation_turns WHERE {where} ORDER BY trace_id", args).fetchall()
        else:
            rows = self._family(scope, memory_id)
            turns = []
        # Include provenance in the fingerprint: a repeated assertion must also
        # invalidate a prior destructive preview, even when its fact ID is reused.
        state = [(row["id"], row["updated_at"], row["content"]) for row in rows]
        for row in rows:
            state.extend(tuple(x) for x in self.db.execute(
                "SELECT memory_id,trace_id,observed_at FROM memory_fact_sources WHERE memory_id=? ORDER BY trace_id", (row["id"],),
            ))
        digest = hashlib.sha256(json.dumps([state, [x[0] for x in turns]], ensure_ascii=False).encode()).hexdigest()
        return {"count": len(rows), "journal_count": len(turns), "fingerprint": digest}

    def _job(self, ids, sessions, user_id=None, scope_session=None):
        job = {"id": uuid.uuid4().hex, "memory_ids": sorted(set(ids)), "session_ids": sorted(set(sessions)),
               "user_id": user_id, "scope_session": scope_session}
        self.db.execute("INSERT INTO memory_cleanup_jobs VALUES (?,?,?)", (job["id"], json.dumps(job), time.time()))
        return job

    def _delete_rows(self, rows):
        ids, sessions = [], set()
        for row in rows:
            memory_id = row["id"]
            ids.append(memory_id)
            sessions.add(row["session_id"])
            item = self.item(row)
            sources = self.db.execute("SELECT trace_id,session_id FROM memory_fact_sources WHERE memory_id=?", (memory_id,)).fetchall()
            sources = [tuple(x) for x in sources] + [(item.metadata.get("trace_id"), row["session_id"])]
            for trace, session in sources:
                if trace:
                    self.db.execute("DELETE FROM conversation_turns WHERE user_id=? AND session_id=? AND trace_id=?", (row["user_id"], session, trace))
                sessions.add(session)
            for table in ("memory_search_terms", "memory_search_documents", "memory_fact_sources", "memory_fact_versions", "memory_edits"):
                self.db.execute(f"DELETE FROM {table} WHERE memory_id=?", (memory_id,))
            self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return ids, sessions

    def delete(self, scope, memory_id=None, fingerprint=None):
        preview = self.preview(scope, memory_id)
        if fingerprint != preview["fingerprint"]:
            raise ValueError("data changed; request a new deletion preview")
        if memory_id is None:
            where, args = self._where(scope)
            rows = self.db.execute(f"SELECT * FROM memories WHERE {where}", args).fetchall()
            sessions = {x[0] for x in self.db.execute(f"SELECT DISTINCT session_id FROM conversation_turns WHERE {where}", args)}
        else:
            rows = self._family(scope, memory_id)
            sessions = set()
        ids, affected = self._delete_rows(rows)
        sessions.update(affected)
        sessions.add(scope.session_id)
        if memory_id is None:
            self.db.execute(f"DELETE FROM conversation_turns WHERE {where}", args)
        return self._job(ids, sessions, scope.user_id, scope.session_id if self.kind == "session" else None)

    def correct(self, scope, memory_id, text, trace_id, timestamp):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("correction must contain 1 to 2000 characters")
        original = self.item(self._row(scope, memory_id))
        fact = self.db.execute("SELECT * FROM memory_fact_versions WHERE memory_id=?", (memory_id,)).fetchone()
        if fact:
            claims = extract_explicit_facts(text)
            if fact["status"] != "active" or len(claims) != 1 or claims[0].key != fact["fact_key"]:
                raise ValueError("correct the current fact with one explicit statement of the same type")
            if timestamp < fact["valid_from"]:
                raise ValueError("correction time precedes the current fact")
            source = replace(original, created_at=timestamp, metadata={**original.metadata, "trace_id": trace_id,
                             "source_type": "user_correction", "corrects_id": memory_id})
            corrected = self.facts.apply(source, claims)[0]
        else:
            previous = self.db.execute("SELECT memory_id FROM memory_edits WHERE trace_id=?", (trace_id,)).fetchone()
            if previous:
                if previous[0] != memory_id:
                    raise ValueError("correction trace was already used")
                return {"memory_id": memory_id, "job": None}
            revision = self.db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM memory_edits WHERE memory_id=?", (memory_id,)).fetchone()[0]
            self.db.execute("INSERT INTO memory_edits VALUES (?,?,?,?,?,?)", (memory_id, revision, original.content, text, timestamp, trace_id))
            corrected = replace(original, content=text, metadata={**original.metadata, "source_type": "user_correction"})
            self.upsert(corrected, require_existing=True)
        return {"memory_id": corrected.id, "job": self._job([], [scope.session_id, original.metadata["session_id"]], scope.user_id,
                                                           scope.session_id if self.kind == "session" else None)}

    def prune(self, now, journal_days=0, memory_days=0, inactive_fact_days=0, pinned_ids=()):
        # Zero means preserve. Pinned records are read before this transaction;
        # the service skips pruning if that read fails.
        pinned = set(pinned_ids)
        rows = self.db.execute("SELECT m.*,f.status,f.valid_until FROM memories m LEFT JOIN memory_fact_versions f ON f.memory_id=m.id").fetchall()
        expired = []
        for row in rows:
            if row["id"] in pinned:
                continue
            if memory_days > 0 and max(row["created_at"], row["updated_at"]) < now - memory_days * 86400:
                expired.append(row)
            elif inactive_fact_days > 0 and row["status"] in {"superseded", "historical"} and (row["valid_until"] or row["created_at"]) < now - inactive_fact_days * 86400:
                expired.append(row)
        ids, sessions = self._delete_rows(expired)
        journal_count = 0
        if journal_days > 0:
            journal_count = self.db.execute("DELETE FROM conversation_turns WHERE created_at<?", (now - journal_days * 86400,)).rowcount
        # Retention does not clear still-current session history; it has its own
        # bounded age. Only explicit corrections/deletions invalidate snapshots.
        return {"deleted_memories": len(ids), "deleted_journal": journal_count,
                "job": self._job(ids, []) if ids else None}

    def jobs(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM memory_cleanup_jobs ORDER BY created_at")]

    def finish(self, job_id):
        self.db.execute("DELETE FROM memory_cleanup_jobs WHERE id=?", (job_id,))
