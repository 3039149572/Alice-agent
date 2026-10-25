from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..drives.models import DriveState
from ..proactive.models import ActionStatus, CandidateAction, ScheduledContextEvent
from ..proactive.attempts import empty_daily_state, reserve_attempt, finish_attempt, recover_attempts


class InMemoryAutonomyStore:
    def __init__(self) -> None:
        self.drives: dict[str, DriveState] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.scheduled: dict[str, ScheduledContextEvent] = {}
        self.sends: list[dict[str, Any]] = []
        self.last_proactive_by_session: dict[str, float] = {}
        self.actions: dict[str, CandidateAction] = {}
        self.daily_proactive: dict[tuple[str, str], dict[str, Any]] = {}
        self.daily_decisions: dict[tuple[str, str], dict[str, Any]] = {}
        self._delivery_operations: set[str] = set()

    async def get_daily_proactive_decision(self, session_id: str, day: str):
        value = self.daily_decisions.get((session_id, day))
        return dict(value) if value is not None else None

    async def save_daily_proactive_decision(self, session_id: str, day: str, value: dict):
        return dict(self.daily_decisions.setdefault((session_id, day), dict(value)))

    async def get_drive(self, session_id: str) -> DriveState | None:
        value = self.drives.get(session_id)
        return DriveState(**asdict(value)) if value else None

    async def save_drive(self, state: DriveState) -> None:
        self.drives[state.session_id] = DriveState(**asdict(state))

    async def upsert_session(self, session_id: str, user_id: str, timestamp: float) -> int:
        item = self.sessions.setdefault(session_id, {"revision": 0, "last_tick_at": timestamp})
        item.update(user_id=user_id, last_interaction_at=timestamp, active=True)
        item["revision"] += 1
        return int(item["revision"])

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(session_id=key, **value) for key, value in self.sessions.items() if value.get("active", True)]

    async def sessions_for_user(self, user_id: str) -> list[str]:
        return [key for key, value in self.sessions.items() if value.get("user_id") == user_id]

    async def invalidate_data_context(self, user_id: str, session_ids: list[str]) -> None:
        sessions = set(session_ids)
        self.scheduled = {key: value for key, value in self.scheduled.items() if not (value.user_id == user_id and value.session_id in sessions)}
        self.actions = {key: value for key, value in self.actions.items() if value.metadata.get("session_id") not in sessions}
        self.daily_decisions = {key: value for key, value in self.daily_decisions.items() if key[0] not in sessions}
        for session in sessions:
            if session in self.sessions:
                self.sessions[session]["revision"] += 1

    async def update_tick(self, session_id: str, timestamp: float) -> None:
        if session_id in self.sessions:
            self.sessions[session_id]["last_tick_at"] = timestamp

    async def get_revision(self, session_id: str) -> int:
        return int(self.sessions.get(session_id, {}).get("revision", 0))

    async def add_scheduled(self, event: ScheduledContextEvent) -> None:
        self.scheduled[event.id] = event

    async def due_scheduled(self, now: float) -> list[ScheduledContextEvent]:
        return [event for event in self.scheduled.values() if event.status == "scheduled" and event.followup_start <= now <= event.followup_end]

    async def mark_scheduled(self, event_id: str, status: str) -> None:
        if event_id in self.scheduled:
            self.scheduled[event_id].status = status

    async def unresolved_count(self, session_id: str, now: float) -> int:
        return sum(
            1 for event in self.scheduled.values()
            if event.session_id == session_id
            and event.status == "scheduled"
            and event.followup_end >= now
        )

    async def record_send(self, user_id: str, session_id: str, topic: str | None, timestamp: float) -> None:
        self.sends.append({"user_id": user_id, "session_id": session_id, "topic": topic or "", "sent_at": timestamp})
        self.last_proactive_by_session[session_id] = float(timestamp)

    async def send_stats(
        self, user_id: str, topic: str | None, day_start: float,
        session_id: str | None = None,
    ) -> dict[str, float | int | None]:
        user = [x for x in self.sends if x["user_id"] == user_id]
        topic_rows = [x for x in user if x["topic"] == (topic or "")]
        return {
            "global_last": max((x["sent_at"] for x in self.sends), default=None),
            "user_last": max((x["sent_at"] for x in user), default=None),
            "session_last": self.last_proactive_by_session.get(session_id) if session_id else None,
            "topic_last": max((x["sent_at"] for x in topic_rows), default=None),
            "daily_count": sum(1 for x in user if x["sent_at"] >= day_start),
        }

    async def get_daily_proactive_state(self, session_id: str, day: str) -> dict[str, Any]:
        return copy.deepcopy({**empty_daily_state(), **self.daily_proactive.get((session_id, day), {})})

    async def claim_daily_proactive_attempt(
        self, session_id: str, day: str, event_id: str, limit: int, **kwargs
    ) -> bool:
        key = (session_id, day)
        value = self.daily_proactive.setdefault(key, empty_daily_state())
        return reserve_attempt(value, event_id, limit, **kwargs)

    async def finish_daily_proactive_attempt(self, session_id, day, event_id, claim_id, **kwargs):
        value = self.daily_proactive.setdefault((session_id, day), empty_daily_state())
        return copy.deepcopy(finish_attempt(value, event_id, claim_id, **kwargs))

    async def recover_daily_proactive_attempts(self, session_id, day, now, **kwargs):
        value = self.daily_proactive.setdefault((session_id, day), empty_daily_state())
        return copy.deepcopy(recover_attempts(value, now, **kwargs))

    async def record_daily_proactive_delivery(
        self, session_id: str, day: str, event_id: str
    ) -> None:
        value = self.daily_proactive.setdefault((session_id, day), {
            "attempt_count": 0, "attempted_event_ids": [], "delivered_event_ids": [],
        })
        if event_id not in value["delivered_event_ids"]:
            value["delivered_event_ids"].append(event_id)

    async def save_action(self, action: CandidateAction) -> None:
        self.actions[action.action_id] = action

    async def cancel_pending(self, session_id: str) -> None:
        for action in self.actions.values():
            if action.metadata.get("session_id") == session_id and action.status.value in {"candidate", "planned", "executing"}:
                action.status = ActionStatus.CANCELLED

    async def commit_delivery_once(self, operation_id: str, payload: dict) -> None:
        if operation_id in self._delivery_operations:
            return
        event, action = payload["event"], dict(payload["action"])
        await self.record_send(event["user_id"], event["session_id"], action.get("topic"), event["timestamp"])
        if payload["daily_day"] and payload["daily_event_id"]:
            await self.record_daily_proactive_delivery(event["session_id"], payload["daily_day"], payload["daily_event_id"])
        action["status"] = ActionStatus(action["status"])
        self.actions[action["action_id"]] = CandidateAction(**action)
        if payload["scheduled_id"] in self.scheduled:
            self.scheduled[payload["scheduled_id"]].status = "evaluated"
        self._delivery_operations.add(operation_id)

    async def close(self) -> None:
        return None

    async def maintenance(self, now: float) -> None:
        self.sends = [row for row in self.sends if row["sent_at"] >= now - 172800.0]
        cutoff = (datetime.fromtimestamp(now).date() - timedelta(days=30)).isoformat()
        self.daily_decisions = {k: v for k, v in self.daily_decisions.items() if k[1] >= cutoff}
        self.daily_proactive = {
            key: value for key, value in self.daily_proactive.items() if key[1] >= cutoff
        }
        for event in self.scheduled.values():
            if event.status == "scheduled" and event.followup_end < now:
                event.status = "expired"


class SQLiteAutonomyStore:
    """Small durable store for drive, cooldown, schedule and action validity state."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drive_states (
                    session_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS autonomy_sessions (
                    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    last_interaction_at REAL NOT NULL, last_tick_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS scheduled_context_events (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, topic TEXT NOT NULL, sent_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_sends_user_time
                    ON proactive_sends(user_id, sent_at);
                CREATE TABLE IF NOT EXISTS proactive_session_state (
                    session_id TEXT PRIMARY KEY, last_sent_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_actions (
                    action_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_proactive_state (
                    session_id TEXT NOT NULL, day TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    attempted_event_ids TEXT NOT NULL DEFAULT '[]',
                    delivered_event_ids TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(session_id,day)
                );
                CREATE TABLE IF NOT EXISTS daily_proactive_decisions (
                    session_id TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(session_id,day)
                );
                CREATE TABLE IF NOT EXISTS daily_proactive_attempt_leases (
                    session_id TEXT NOT NULL, day TEXT NOT NULL, event_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(session_id,day,event_id)
                );
                CREATE TABLE IF NOT EXISTS autonomy_delivery_operations (
                    operation_id TEXT PRIMARY KEY
                );
            """)

    async def sessions_for_user(self, user_id: str) -> list[str]:
        def query():
            with closing(self._connect()) as db:
                return [row[0] for row in db.execute("SELECT session_id FROM autonomy_sessions WHERE user_id=?", (user_id,))]
        return await self._run(query)

    async def invalidate_data_context(self, user_id: str, session_ids: list[str]) -> None:
        def query():
            with closing(self._connect()) as db, db:
                for session in set(session_ids):
                    db.execute("DELETE FROM proactive_actions WHERE session_id=?", (session,))
                    db.execute("DELETE FROM daily_proactive_decisions WHERE session_id=?", (session,))
                    db.execute("UPDATE autonomy_sessions SET revision=revision+1 WHERE user_id=? AND session_id=?", (user_id, session))
                for row in db.execute("SELECT id,payload FROM scheduled_context_events").fetchall():
                    value = json.loads(row["payload"])
                    if value.get("user_id") == user_id and value.get("session_id") in session_ids:
                        db.execute("DELETE FROM scheduled_context_events WHERE id=?", (row["id"],))
        await self._run(query)

    async def commit_delivery_once(self, operation_id: str, payload: dict) -> None:
        def query():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM autonomy_delivery_operations WHERE operation_id=?", (operation_id,)).fetchone():
                    return
                event, action = payload["event"], payload["action"]
                session, stamp = event["session_id"], event["timestamp"]
                db.execute("INSERT INTO proactive_sends(user_id,session_id,topic,sent_at) VALUES (?,?,?,?)", (event["user_id"], session, action.get("topic") or "", stamp))
                db.execute("INSERT INTO proactive_session_state VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET last_sent_at=MAX(last_sent_at,excluded.last_sent_at)", (session, stamp))
                day, daily_id = payload["daily_day"], payload["daily_event_id"]
                if day and daily_id:
                    db.execute("INSERT OR IGNORE INTO daily_proactive_state(session_id,day) VALUES (?,?)", (session, day))
                    row = db.execute("SELECT delivered_event_ids FROM daily_proactive_state WHERE session_id=? AND day=?", (session, day)).fetchone()
                    delivered = json.loads(row[0])
                    if daily_id not in delivered:
                        delivered.append(daily_id)
                    db.execute("UPDATE daily_proactive_state SET delivered_event_ids=? WHERE session_id=? AND day=?", (json.dumps(delivered), session, day))
                db.execute("INSERT INTO proactive_actions VALUES (?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                           (action["action_id"], session, action["status"], json.dumps(action, ensure_ascii=False)))
                if payload["scheduled_id"]:
                    row = db.execute("SELECT payload FROM scheduled_context_events WHERE id=?", (payload["scheduled_id"],)).fetchone()
                    if row:
                        scheduled = json.loads(row[0])
                        scheduled["status"] = "evaluated"
                        db.execute("UPDATE scheduled_context_events SET status='evaluated',payload=? WHERE id=?", (json.dumps(scheduled, ensure_ascii=False), payload["scheduled_id"]))
                db.execute("INSERT INTO autonomy_delivery_operations VALUES (?)", (operation_id,))
        await self._run(query)

    async def get_daily_proactive_decision(self, session_id: str, day: str):
        def query():
            with closing(self._connect()) as db:
                row = db.execute("SELECT payload FROM daily_proactive_decisions WHERE session_id=? AND day=?",
                                 (session_id, day)).fetchone()
                return json.loads(row[0]) if row else None
        return await self._run(query)

    async def save_daily_proactive_decision(self, session_id: str, day: str, value: dict):
        def query():
            with closing(self._connect()) as db, db:
                db.execute("INSERT OR IGNORE INTO daily_proactive_decisions VALUES(?,?,?)",
                           (session_id, day, json.dumps(value, ensure_ascii=False)))
                row = db.execute("SELECT payload FROM daily_proactive_decisions WHERE session_id=? AND day=?",
                                 (session_id, day)).fetchone()
                return json.loads(row[0])
        return await self._run(query)

    async def _run(self, function):
        async with self._lock:
            return await asyncio.to_thread(function)

    async def get_drive(self, session_id: str) -> DriveState | None:
        def query():
            with closing(self._connect()) as db, db:
                row = db.execute("SELECT payload FROM drive_states WHERE session_id=?", (session_id,)).fetchone()
                return DriveState(**json.loads(row[0])) if row else None
        return await self._run(query)

    async def save_drive(self, state: DriveState) -> None:
        payload = json.dumps(asdict(state), ensure_ascii=False)
        def query():
            with closing(self._connect()) as db, db:
                db.execute("INSERT INTO drive_states(session_id,payload) VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload", (state.session_id, payload))
        await self._run(query)

    async def upsert_session(self, session_id: str, user_id: str, timestamp: float) -> int:
        def query():
            with closing(self._connect()) as db, db:
                db.execute("""INSERT INTO autonomy_sessions(session_id,user_id,last_interaction_at,last_tick_at,revision,active)
                    VALUES(?,?,?,?,1,1) ON CONFLICT(session_id) DO UPDATE SET
                    user_id=excluded.user_id,last_interaction_at=excluded.last_interaction_at,
                    revision=autonomy_sessions.revision+1,active=1""", (session_id,user_id,timestamp,timestamp))
                return int(db.execute("SELECT revision FROM autonomy_sessions WHERE session_id=?", (session_id,)).fetchone()[0])
        return await self._run(query)

    async def list_sessions(self) -> list[dict[str, Any]]:
        def query():
            with closing(self._connect()) as db, db:
                return [dict(row) for row in db.execute("SELECT * FROM autonomy_sessions WHERE active=1")]
        return await self._run(query)

    async def update_tick(self, session_id: str, timestamp: float) -> None:
        await self._run(lambda: self._update("UPDATE autonomy_sessions SET last_tick_at=? WHERE session_id=?", (timestamp, session_id)))

    def _update(self, sql: str, values: tuple[Any, ...]) -> None:
        with closing(self._connect()) as db, db:
            db.execute(sql, values)

    async def get_revision(self, session_id: str) -> int:
        def query():
            with closing(self._connect()) as db, db:
                row = db.execute("SELECT revision FROM autonomy_sessions WHERE session_id=?", (session_id,)).fetchone()
                return int(row[0]) if row else 0
        return await self._run(query)

    async def add_scheduled(self, event: ScheduledContextEvent) -> None:
        payload = json.dumps(asdict(event), ensure_ascii=False)
        await self._run(lambda: self._update("INSERT INTO scheduled_context_events(id,payload,status) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status", (event.id,payload,event.status)))

    async def due_scheduled(self, now: float) -> list[ScheduledContextEvent]:
        def query():
            with closing(self._connect()) as db, db:
                rows = db.execute("SELECT payload FROM scheduled_context_events WHERE status='scheduled'").fetchall()
                values = [ScheduledContextEvent(**json.loads(row[0])) for row in rows]
                return [event for event in values if event.followup_start <= now <= event.followup_end]
        return await self._run(query)

    async def mark_scheduled(self, event_id: str, status: str) -> None:
        await self._run(lambda: self._update("UPDATE scheduled_context_events SET status=? WHERE id=?", (status,event_id)))

    async def unresolved_count(self, session_id: str, now: float) -> int:
        def query():
            with closing(self._connect()) as db, db:
                rows = db.execute("SELECT payload FROM scheduled_context_events WHERE status='scheduled'").fetchall()
                events = [ScheduledContextEvent(**json.loads(row[0])) for row in rows]
                return sum(
                    1 for event in events
                    if event.session_id == session_id and event.followup_end >= now
                )
        return await self._run(query)

    async def record_send(self, user_id: str, session_id: str, topic: str | None, timestamp: float) -> None:
        def query():
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO proactive_sends(user_id,session_id,topic,sent_at) VALUES(?,?,?,?)",
                    (user_id, session_id, topic or "", timestamp),
                )
                db.execute(
                    "INSERT INTO proactive_session_state(session_id,last_sent_at) VALUES(?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET last_sent_at=excluded.last_sent_at",
                    (session_id, timestamp),
                )
        await self._run(query)

    async def send_stats(
        self, user_id: str, topic: str | None, day_start: float,
        session_id: str | None = None,
    ) -> dict[str, float | int | None]:
        def query():
            with closing(self._connect()) as db, db:
                global_last = db.execute("SELECT MAX(sent_at) FROM proactive_sends").fetchone()[0]
                user_last = db.execute("SELECT MAX(sent_at) FROM proactive_sends WHERE user_id=?", (user_id,)).fetchone()[0]
                session_last = None
                if session_id:
                    state_row = db.execute(
                        "SELECT last_sent_at FROM proactive_session_state WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                    session_last = state_row[0] if state_row else None
                topic_last = db.execute("SELECT MAX(sent_at) FROM proactive_sends WHERE user_id=? AND topic=?", (user_id,topic or "")).fetchone()[0]
                daily_count = db.execute("SELECT COUNT(*) FROM proactive_sends WHERE user_id=? AND sent_at>=?", (user_id,day_start)).fetchone()[0]
                return {"global_last":global_last,"user_last":user_last,"session_last":session_last,"topic_last":topic_last,"daily_count":int(daily_count)}
        return await self._run(query)

    async def get_daily_proactive_state(self, session_id: str, day: str) -> dict[str, Any]:
        def query():
            with closing(self._connect()) as db:
                return self._read_daily_state(db, session_id, day)
        return await self._run(query)

    @staticmethod
    def _read_daily_state(db, session_id, day):
        state = empty_daily_state()
        row = db.execute("SELECT attempt_count,attempted_event_ids,delivered_event_ids "
                         "FROM daily_proactive_state WHERE session_id=? AND day=?", (session_id, day)).fetchone()
        if row is not None:
            state.update(attempt_count=int(row["attempt_count"]),
                         attempted_event_ids=json.loads(row["attempted_event_ids"]),
                         delivered_event_ids=json.loads(row["delivered_event_ids"]))
        state["event_attempts"] = {row["event_id"]: json.loads(row["payload"]) for row in db.execute(
            "SELECT event_id,payload FROM daily_proactive_attempt_leases WHERE session_id=? AND day=?",
            (session_id, day))}
        return state

    async def _mutate_daily_state(self, session_id, day, mutate):
        def query():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                state = self._read_daily_state(db, session_id, day)
                result = mutate(state)
                db.execute("INSERT INTO daily_proactive_state(session_id,day,attempt_count,attempted_event_ids,delivered_event_ids) "
                           "VALUES(?,?,?,?,?) ON CONFLICT(session_id,day) DO UPDATE SET "
                           "attempt_count=excluded.attempt_count,attempted_event_ids=excluded.attempted_event_ids,"
                           "delivered_event_ids=excluded.delivered_event_ids",
                           (session_id, day, state["attempt_count"], json.dumps(state["attempted_event_ids"]),
                            json.dumps(state["delivered_event_ids"])))
                db.executemany("INSERT INTO daily_proactive_attempt_leases(session_id,day,event_id,payload) VALUES(?,?,?,?) "
                               "ON CONFLICT(session_id,day,event_id) DO UPDATE SET payload=excluded.payload",
                               [(session_id, day, event_id, json.dumps(attempt)) for event_id, attempt in state["event_attempts"].items()])
                return result
        return await self._run(query)

    async def claim_daily_proactive_attempt(
        self, session_id: str, day: str, event_id: str, limit: int, **kwargs
    ) -> bool:
        return await self._mutate_daily_state(session_id, day, lambda state: reserve_attempt(state, event_id, limit, **kwargs))

    async def finish_daily_proactive_attempt(self, session_id, day, event_id, claim_id, **kwargs):
        return await self._mutate_daily_state(session_id, day, lambda state: finish_attempt(state, event_id, claim_id, **kwargs))

    async def recover_daily_proactive_attempts(self, session_id, day, now, **kwargs):
        return await self._mutate_daily_state(session_id, day, lambda state: recover_attempts(state, now, **kwargs))

    async def record_daily_proactive_delivery(
        self, session_id: str, day: str, event_id: str
    ) -> None:
        def query():
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT OR IGNORE INTO daily_proactive_state(session_id,day) VALUES(?,?)",
                    (session_id, day),
                )
                row = db.execute(
                    "SELECT delivered_event_ids FROM daily_proactive_state "
                    "WHERE session_id=? AND day=?", (session_id, day),
                ).fetchone()
                delivered = json.loads(row["delivered_event_ids"])
                if event_id not in delivered:
                    delivered.append(event_id)
                    db.execute(
                        "UPDATE daily_proactive_state SET delivered_event_ids=? "
                        "WHERE session_id=? AND day=?",
                        (json.dumps(delivered), session_id, day),
                    )
        await self._run(query)

    async def save_action(self, action: CandidateAction) -> None:
        session_id = str(action.metadata.get("session_id", ""))
        payload = json.dumps(asdict(action), ensure_ascii=False, default=str)
        await self._run(lambda: self._update("INSERT INTO proactive_actions(action_id,session_id,status,payload) VALUES(?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET status=excluded.status,payload=excluded.payload", (action.action_id,session_id,action.status.value,payload)))

    async def cancel_pending(self, session_id: str) -> None:
        await self._run(lambda: self._update("UPDATE proactive_actions SET status='cancelled' WHERE session_id=? AND status IN ('candidate','planned','executing')", (session_id,)))

    async def close(self) -> None:
        return None

    async def maintenance(self, now: float) -> None:
        cutoff = (datetime.fromtimestamp(now).date() - timedelta(days=30)).isoformat()
        def query():
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM proactive_sends WHERE sent_at<?", (now - 172800.0,))
                db.execute("DELETE FROM daily_proactive_state WHERE day<?", (cutoff,))
                db.execute("DELETE FROM daily_proactive_decisions WHERE day<?", (cutoff,))
                db.execute("DELETE FROM daily_proactive_attempt_leases WHERE day<?", (cutoff,))
                rows = db.execute("SELECT id,payload FROM scheduled_context_events WHERE status='scheduled'").fetchall()
                for row in rows:
                    event = ScheduledContextEvent(**json.loads(row["payload"]))
                    if event.followup_end < now:
                        db.execute("UPDATE scheduled_context_events SET status='expired' WHERE id=?", (row["id"],))
        await self._run(query)
