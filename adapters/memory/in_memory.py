from __future__ import annotations

from pathlib import Path
from .local_sqlite import LocalSQLiteMemoryAdapter, LocalSQLiteMemoryConfig


class InMemoryMemoryAdapter(LocalSQLiteMemoryAdapter):
    """Development-only SQLite :memory: store with the same fact/recall policy.

    No database file or journal is persisted. Session isolation is preserved.
    Sharing the implementation prevents production and fallback memory rules
    from diverging.
    """

    def __init__(self) -> None:
        super().__init__(LocalSQLiteMemoryConfig(
            database_path=Path(":memory:"), scope="session", capture_all_turns=False,
            min_score=0.0,
        ))

    async def count(self) -> int:
        if self._closed:
            return 0
        memories, _ = await self.counts()
        return memories
