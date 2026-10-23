from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from ..domain.history import ConversationTurn
from ..ports.logging import LoggerPort, NullLogger
from ..ports.storage import StoragePort


_HISTORY_NAMESPACE = "alice_session_history"
_HISTORY_SCHEMA = "alice_session_history_v1"
_DEFAULT_MAX_AGE_SECONDS = 72 * 60 * 60
_MAX_PERSISTED_TURNS = 64
_MAX_PERSISTED_TEXT_CHARS = 8000
_MAX_FUTURE_SKEW_SECONDS = 300.0


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class SessionManager:
    """Owns per-session locks and bounded recent history.

    Storage is deliberately optional so the framework-independent core can
    retain its original in-memory behavior when persistence is disabled.
    """

    def __init__(
        self,
        history_limit: int = 8,
        *,
        storage: StoragePort | None = None,
        logger: LoggerPort | None = None,
        clock: Callable[[], float] = time.time,
        max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self._history_limit = max(0, int(history_limit))
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._histories: dict[str, deque[ConversationTurn]] = {}
        self._storage = storage
        self._logger = logger or NullLogger()
        self._clock = clock
        max_age = _finite_number(max_age_seconds)
        self._max_age_seconds = (
            max_age if max_age is not None and max_age > 0
            else float(_DEFAULT_MAX_AGE_SECONDS)
        )
        self._io_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._loaded_sessions: set[str] = set()
        # A failed read must not lead to overwriting an unread snapshot with an
        # incomplete in-memory history. Writes can still be retried after a
        # write failure once the initial read has succeeded.
        self._writable_sessions: set[str] = set()
        self._pending_writes: set[asyncio.Task[None]] = set()
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks[session_id]

    def _now(self) -> float:
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            now = float("nan")
        return now if math.isfinite(now) else time.time()

    def _is_recent(self, turn: ConversationTurn, now: float) -> bool:
        if (
            not isinstance(getattr(turn, "user_text", None), str)
            or (
                getattr(turn, "assistant_text", None) is not None
                and not isinstance(turn.assistant_text, str)
            )
        ):
            return False
        timestamp = _finite_number(getattr(turn, "timestamp", None))
        return (
            timestamp is not None
            and timestamp <= now + _MAX_FUTURE_SKEW_SECONDS
            and max(0.0, now - timestamp) <= self._max_age_seconds
        )

    def _prune_history(self, session_id: str, now: float | None = None) -> None:
        history = self._histories.get(session_id)
        if history is None or self._storage is None:
            return
        checked_at = self._now() if now is None else now
        retained = [turn for turn in history if self._is_recent(turn, checked_at)]
        if len(retained) == len(history):
            return
        replacement = deque(retained, maxlen=self._history_limit)
        if replacement:
            self._histories[session_id] = replacement
        else:
            self._histories.pop(session_id, None)

    def history(self, session_id: str) -> list[ConversationTurn]:
        if self._history_limit <= 0:
            return []
        self._prune_history(session_id)
        history = self._histories.get(session_id)
        return list(history) if history else []

    def append(self, session_id: str, turn: ConversationTurn) -> None:
        """Append in memory for compatibility with existing core consumers.

        Production delivery paths use ``append_and_persist`` so they can load
        prior history before changing the bounded snapshot.
        """
        if self._history_limit <= 0:
            return
        now = self._now()
        if self._storage is not None and not self._is_recent(turn, now):
            return
        history = self._histories.setdefault(
            session_id, deque(maxlen=self._history_limit)
        )
        if not turn.delivery_id or all(item.delivery_id != turn.delivery_id for item in history):
            history.append(turn)

    async def load_history(self, session_id: str) -> list[ConversationTurn]:
        if self._history_limit <= 0:
            return []
        if self._storage is None or self._closing:
            return self.history(session_id)
        async with self._io_locks[session_id]:
            await self._load_locked(session_id)
            self._prune_history(session_id)
            history = self._histories.get(session_id)
            return list(history) if history else []

    async def _load_locked(self, session_id: str) -> None:
        if session_id in self._loaded_sessions or self._storage is None:
            return
        try:
            snapshot = await self._storage.get(
                _HISTORY_NAMESPACE, self._storage_key(session_id)
            )
        except asyncio.CancelledError:
            raise
        except json.JSONDecodeError as exc:
            # SQLite is readable but this session row is malformed. Treat it
            # like a corrupt snapshot so the next committed turn can replace it.
            self._loaded_sessions.add(session_id)
            self._writable_sessions.add(session_id)
            self._logger.warning(
                f"Alice session history snapshot could not be decoded ({type(exc).__name__})"
            )
            return
        except Exception as exc:
            self._loaded_sessions.add(session_id)
            self._logger.warning(
                f"Alice session history restore failed ({type(exc).__name__})"
            )
            return

        self._loaded_sessions.add(session_id)
        self._writable_sessions.add(session_id)
        if snapshot is None:
            return
        turns = self._decode_snapshot(snapshot)
        if turns is None:
            self._logger.warning("Alice session history snapshot is invalid; ignoring it")
            return
        now = self._now()
        turns.extend(
            turn for turn in self._histories.get(session_id, ())
            if self._is_recent(turn, now)
        )
        if turns:
            self._histories[session_id] = deque(turns, maxlen=self._history_limit)

    def _decode_snapshot(self, snapshot: Any) -> list[ConversationTurn] | None:
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"schema_version", "turns"}
            or snapshot.get("schema_version") != _HISTORY_SCHEMA
            or not isinstance(snapshot.get("turns"), list)
        ):
            return None

        now = self._now()
        restored: list[ConversationTurn] = []
        invalid_entry = False
        max_scan = max(256, min(self._history_limit, _MAX_PERSISTED_TURNS) * 4)
        for item in snapshot["turns"][-max_scan:]:
            if (
                not isinstance(item, dict)
                or set(item) not in ({"user_text", "assistant_text", "timestamp"}, {"user_text", "assistant_text", "timestamp", "delivery_id"})
            ):
                invalid_entry = True
                continue
            user_text = item.get("user_text")
            assistant_text = item.get("assistant_text")
            timestamp = _finite_number(item.get("timestamp"))
            delivery_id = item.get("delivery_id")
            if (
                not isinstance(user_text, str)
                or (assistant_text is not None and not isinstance(assistant_text, str))
                or timestamp is None
                or timestamp > now + _MAX_FUTURE_SKEW_SECONDS
                or (delivery_id is not None and (not isinstance(delivery_id, str) or len(delivery_id) > 128))
            ):
                invalid_entry = True
                continue
            if max(0.0, now - timestamp) > self._max_age_seconds:
                continue
            restored.append(ConversationTurn(
                user_text=user_text[:_MAX_PERSISTED_TEXT_CHARS],
                assistant_text=(
                    assistant_text[:_MAX_PERSISTED_TEXT_CHARS]
                    if assistant_text is not None else None
                ),
                timestamp=timestamp,
                delivery_id=delivery_id,
            ))

        if invalid_entry:
            self._logger.warning(
                "Alice session history snapshot contained invalid turns"
            )
        return restored[-min(self._history_limit, _MAX_PERSISTED_TURNS):]

    async def append_and_persist(
        self, session_id: str, turn: ConversationTurn, *, strict: bool = False
    ) -> None:
        if self._history_limit <= 0:
            return
        if self._storage is None or self._closing:
            self.append(session_id, turn)
            return
        async with self._io_locks[session_id]:
            if strict and session_id not in self._writable_sessions:
                self._loaded_sessions.discard(session_id)
            await self._load_locked(session_id)
            now = self._now()
            self._prune_history(session_id, now)
            if not self._is_recent(turn, now):
                return
            history = self._histories.setdefault(
                session_id, deque(maxlen=self._history_limit)
            )
            if not turn.delivery_id or all(item.delivery_id != turn.delivery_id for item in history):
                history.append(turn)
            if session_id not in self._writable_sessions:
                if strict:
                    raise OSError("session history is unreadable; commit will retry")
                return
            await self._persist_locked(session_id, strict=strict)

    async def _persist_locked(self, session_id: str, *, strict: bool = False) -> None:
        if self._storage is None or session_id not in self._writable_sessions:
            return
        now = self._now()
        self._prune_history(session_id, now)
        count = min(self._history_limit, _MAX_PERSISTED_TURNS)
        recent = list(self._histories.get(session_id, ()))[-count:]
        snapshot = {
            "schema_version": _HISTORY_SCHEMA,
            "turns": [
                {
                    "user_text": turn.user_text[:_MAX_PERSISTED_TEXT_CHARS],
                    "assistant_text": (
                        turn.assistant_text[:_MAX_PERSISTED_TEXT_CHARS]
                        if turn.assistant_text is not None else None
                    ),
                    "timestamp": float(turn.timestamp),
                    **({"delivery_id": turn.delivery_id} if turn.delivery_id else {}),
                }
                for turn in recent
            ],
        }
        write_task = asyncio.create_task(
            self._storage.set(
                _HISTORY_NAMESPACE, self._storage_key(session_id), snapshot
            )
        )
        self._pending_writes.add(write_task)
        write_task.add_done_callback(self._pending_writes.discard)
        cancellation_requested = False
        while not write_task.done():
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                cancellation_requested = True
                current = asyncio.current_task()
                if current is not None and hasattr(current, "uncancel"):
                    current.uncancel()
            except Exception:
                break

        write_error: BaseException | None = None
        if write_task.cancelled():
            write_error = asyncio.CancelledError()
        else:
            write_error = write_task.exception()

        if cancellation_requested:
            if write_error is not None and not isinstance(write_error, asyncio.CancelledError):
                self._logger.warning(
                    f"Alice session history write failed ({type(write_error).__name__})"
                )
            raise asyncio.CancelledError
        if write_error is not None:
            if isinstance(write_error, asyncio.CancelledError):
                raise write_error
            if strict:
                raise write_error
            self._logger.warning(
                f"Alice session history write failed ({type(write_error).__name__})"
            )

    @staticmethod
    def _storage_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    async def forget(self, session_id: str) -> None:
        """Erase the cached and persisted snapshot after a data-management action.

        The caller holds its user data gate, so a pending generation/delivery
        cannot subsequently write a snapshot made before this invalidation.
        """
        async with self._io_locks[session_id]:
            if self._storage is not None:
                await self._storage.delete(_HISTORY_NAMESPACE, self._storage_key(session_id))
            self._histories.pop(session_id, None)
            self._loaded_sessions.discard(session_id)
            self._writable_sessions.discard(session_id)

    async def maintenance(self) -> None:
        """Expire persisted snapshots even when their sessions never return."""
        if self._storage is None or not hasattr(self._storage, "transform_namespace"):
            return
        now = self._now()
        def prune(snapshot):
            if not isinstance(snapshot, dict) or snapshot.get("schema_version") != _HISTORY_SCHEMA or not isinstance(snapshot.get("turns"), list):
                return snapshot
            turns = []
            for turn in snapshot["turns"]:
                stamp = _finite_number(turn.get("timestamp")) if isinstance(turn, dict) else None
                if stamp is not None and now - self._max_age_seconds <= stamp <= now + _MAX_FUTURE_SKEW_SECONDS:
                    turns.append(turn)
            return {**snapshot, "turns": turns} if turns else None
        await self._storage.transform_namespace(_HISTORY_NAMESPACE, prune)
        for session_id in tuple(self._histories):
            self._prune_history(session_id, now)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            locks = list(self._io_locks.values())
            acquired: list[asyncio.Lock] = []
            cancellation_requested = False
            try:
                for lock in locks:
                    await lock.acquire()
                    acquired.append(lock)
                for task in tuple(self._pending_writes):
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            cancellation_requested = True
                            current = asyncio.current_task()
                            if current is not None and hasattr(current, "uncancel"):
                                current.uncancel()
                        except Exception:
                            break
                if self._storage is not None:
                    close_task = asyncio.create_task(self._storage.close())
                    while not close_task.done():
                        try:
                            await asyncio.shield(close_task)
                        except asyncio.CancelledError:
                            cancellation_requested = True
                            current = asyncio.current_task()
                            if current is not None and hasattr(current, "uncancel"):
                                current.uncancel()
                        except Exception:
                            break
                    if close_task.cancelled():
                        self._logger.warning("Alice session history storage close was cancelled")
                    elif close_task.exception() is not None:
                        exc = close_task.exception()
                        self._logger.warning(
                            f"Alice session history storage close failed ({type(exc).__name__})"
                        )
                self._closed = True
            finally:
                for lock in reversed(acquired):
                    lock.release()
            if cancellation_requested:
                raise asyncio.CancelledError
