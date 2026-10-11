from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.relationship.manager import RelationshipManager
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState
from astrbot_plugin_alice_agent.alice_agent.relationship.reducer import RelationshipReducer
from astrbot_plugin_alice_agent.alice_agent.relationship.store import (
    InMemoryRelationshipStore,
    SQLiteRelationshipStore,
)


def event(user_id: str, timestamp: float = 1.0) -> AgentEvent:
    return AgentEvent("e", "trace", "private", "test", "s", user_id, timestamp, "你好")


class RelationshipTests(unittest.IsolatedAsyncioTestCase):
    async def test_users_are_isolated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relationship.db"
            manager = RelationshipManager(
                SQLiteRelationshipStore(path), RelationshipReducer(0.02)
            )
            await manager.update(event("a"), PerceptionResult())
            self.assertEqual((await manager.get("a")).interaction_count, 1)
            self.assertEqual((await manager.get("b")).interaction_count, 0)
            await manager.close()
            reopened = RelationshipManager(
                SQLiteRelationshipStore(path), RelationshipReducer(0.02)
            )
            self.assertEqual((await reopened.get("a")).interaction_count, 1)
            await reopened.close()

    async def test_familiarity_grows_gradually_with_bounded_delta(self) -> None:
        reducer = RelationshipReducer(0.02)
        state = RelationshipState("u")
        previous = state.familiarity
        for index in range(20):
            state = reducer.reduce(state, PerceptionResult(), event("u", index + 1))
            self.assertLessEqual(state.familiarity - previous, 0.02 + 1e-9)
            previous = state.familiarity
        self.assertGreater(state.familiarity, 0.0)
        self.assertLess(state.familiarity, 1.0)

    async def test_single_conflict_cannot_collapse_trust(self) -> None:
        signals = PerceptionResult().social_signals
        signals["conflict"] = 1.0
        old = RelationshipState("u", trust=0.8)
        new = RelationshipReducer(0.02).reduce(
            old, PerceptionResult(social_signals=signals), event("u")
        )
        self.assertGreaterEqual(new.trust, 0.78)

    async def test_same_user_concurrent_updates_do_not_lose_interactions(self) -> None:
        manager = RelationshipManager(
            InMemoryRelationshipStore(), RelationshipReducer(0.02)
        )
        await asyncio.gather(*(
            manager.update(event("u", index + 1), PerceptionResult())
            for index in range(20)
        ))
        self.assertEqual((await manager.get("u")).interaction_count, 20)


if __name__ == "__main__":
    unittest.main()
