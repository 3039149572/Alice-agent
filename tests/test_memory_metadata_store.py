from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryMetadata
from astrbot_plugin_alice_agent.alice_agent.memory.metadata_store import SQLiteMemoryMetadataStore


class MemoryMetadataStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_metadata_and_tracks_access_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intelligence.db"
            store = SQLiteMemoryMetadataStore(path)
            await store.save(MemoryMetadata(
                memory_id="m1", importance=0.8, emotional_weight=0.4,
                relationship_weight=0.6, confidence=0.9, pinned=True,
                decay_rate=0.0, created_at=10.0,
            ))
            await store.record_access(["m1"], 20.0)
            loaded = (await store.get_many(["m1"]))["m1"]
            self.assertEqual(loaded.access_count, 1)
            self.assertEqual(loaded.last_accessed_at, 20.0)
            self.assertTrue(loaded.pinned)
            await store.close()

            connection = sqlite3.connect(path)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(memory_metadata)"
                )
            }
            connection.close()
            self.assertNotIn("content", columns)


if __name__ == "__main__":
    unittest.main()
