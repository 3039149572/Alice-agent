from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


_SENSITIVE_KEYS = {
    "api_key", "authorization", "password", "secret", "access_token",
    "refresh_token", "bearer_token",
}


def _json_safe(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if lowered in _SENSITIVE_KEYS or lowered.endswith("_api_key") or lowered.endswith("_token"):
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(item_key): _json_safe(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key).lower() not in _SENSITIVE_KEYS
            and not str(item_key).lower().endswith("_api_key")
            and not str(item_key).lower().endswith("_token")
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


class AtomicJSONTurnLogger:
    """Write one complete JSON diagnostic record per turn using atomic replace."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = float("-inf")

    def cleanup_expired(self, retention_days: int, *, now: float | None = None) -> int:
        """Opt-in retention, restricted to regular files created by this logger."""
        if retention_days <= 0:
            return 0
        current = time.time() if now is None else now
        removed = 0
        base = self._directory.resolve()
        cutoff = current - retention_days * 86400
        for path in self._directory.glob("*.json"):
            if not re.fullmatch(r"\d{8}T\d{6}_\d{6}Z_[A-Za-z0-9_.-]{1,80}_[0-9a-f]{8}\.json", path.name):
                continue
            try:
                if path.is_symlink() or path.resolve().parent != base or not path.is_file():
                    continue
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _filename(record: dict[str, Any]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        trace = re.sub(r"[^A-Za-z0-9_.-]", "_", str(record.get("trace_id", "turn")))[:80] or "turn"
        return f"{stamp}_{trace}_{uuid4().hex[:8]}.json"

    def write(
        self,
        record: dict[str, Any],
        *,
        enabled: Callable[[], bool] | None = None,
        retention_days: int = 0,
    ) -> Path | None:
        if enabled is not None and not enabled():
            return None
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / self._filename(record)
        temp_name: str | None = None
        safe_record = _json_safe(record)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=self._directory,
                delete=False,
            ) as stream:
                temp_name = stream.name
                json.dump(safe_record, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if enabled is not None and not enabled():
                return None
            os.replace(temp_name, target)
            temp_name = None
            if retention_days > 0 and (enabled is None or enabled()):
                # Never scan the directory for every turn or run two scans at once.
                with self._cleanup_lock:
                    current = time.monotonic()
                    if current - self._last_cleanup >= 3600:
                        try:
                            self.cleanup_expired(retention_days)
                        except OSError:
                            pass
                        self._last_cleanup = current
            return target
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
