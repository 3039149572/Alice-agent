from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ...alice_agent.config import AgentConfig
from ...alice_agent.ports.logging import LoggerPort
from ...alice_agent.ports.memory import MemoryPort
from .in_memory import InMemoryMemoryAdapter
from .local_sqlite import LocalSQLiteMemoryAdapter, LocalSQLiteMemoryConfig


def build_memory_port(
    agent_config: AgentConfig,
    plugin_config: dict[str, Any],
    logger: LoggerPort,
    data_dir: Path,
) -> MemoryPort:
    backend = agent_config.memory_backend.strip().lower()
    if backend == "in_memory":
        return InMemoryMemoryAdapter()
    if backend != "local_sqlite":
        logger.warning(
            f"Unsupported Alice memory backend {backend!r}; using local_sqlite"
        )

    configured_path = str(plugin_config.get("local_memory_path") or "").strip()
    database_path = Path(configured_path) if configured_path else data_dir / "alice_memory.db"
    try:
        config = LocalSQLiteMemoryConfig(
            database_path=database_path,
            scope=str(plugin_config.get("local_memory_scope") or "user"),
            capture_all_turns=bool(
                plugin_config.get("local_memory_capture_all_turns", True)
            ),
            candidate_limit=int(plugin_config.get("local_memory_candidate_limit", 300)),
            min_score=0.0,
            busy_timeout=float(plugin_config.get("local_memory_busy_timeout", 5.0)),
        )
        adapter = LocalSQLiteMemoryAdapter(config)
    except (OSError, ValueError, sqlite3.Error) as exc:
        logger.error(
            "Alice local SQLite memory could not start; using volatile local "
            f"memory for this process: {type(exc).__name__}: {exc}"
        )
        return InMemoryMemoryAdapter()
    logger.info(
        "Alice memory backend=local_sqlite; "
        f"path={config.database_path} scope={config.scope} "
        f"capture_all_turns={config.capture_all_turns}; network=disabled"
    )
    return adapter
