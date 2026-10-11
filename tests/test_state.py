from __future__ import annotations

import unittest

from astrbot_plugin_alice_agent.adapters.storage.in_memory import InMemoryStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult, empty_social_signals
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer


def event(session: str) -> AgentEvent:
    return AgentEvent("e", "t", "private", "test", session, "u", 1.0, "hello")


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = StateManager(InMemoryStorageAdapter(), StateReducer())

    async def test_state_values_stay_in_range(self) -> None:
        perception = PerceptionResult(
            valence=1.0,
            arousal=1.0,
            social_signals={key: 1.0 for key in empty_social_signals()},
            confidence=1.0,
        )
        current = AgentState(mood_valence=0.99, mood_arousal=0.99, energy=0.99)
        reduced = StateReducer().reduce(current, perception, event("a"))
        self.assertLessEqual(reduced.mood_valence, 1.0)
        self.assertLessEqual(reduced.mood_arousal, 1.0)
        self.assertLessEqual(reduced.energy, 1.0)
        self.assertLessEqual(abs(reduced.mood_valence - current.mood_valence), 0.2)
        self.assertLessEqual(abs(reduced.mood_arousal - current.mood_arousal), 0.2)
        self.assertLessEqual(abs(reduced.energy - current.energy), 0.08)

        perception.valence = -1.0
        perception.arousal = 0.0
        current = AgentState(mood_valence=-0.99, mood_arousal=0.0, energy=0.0)
        reduced = StateReducer().reduce(current, perception, event("a"))
        self.assertGreaterEqual(reduced.mood_valence, -1.0)
        self.assertGreaterEqual(reduced.mood_arousal, 0.0)
        self.assertGreaterEqual(reduced.energy, 0.0)

    async def test_sessions_do_not_share_state(self) -> None:
        perception = PerceptionResult(topic="topic-a", confidence=1.0)
        await self.manager.update(event("session-a"), perception)
        state_a = await self.manager.get("session-a")
        state_b = await self.manager.get("session-b")
        self.assertEqual(state_a.current_topic, "topic-a")
        self.assertIsNone(state_b.current_topic)


if __name__ == "__main__":
    unittest.main()
