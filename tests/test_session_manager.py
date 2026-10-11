from __future__ import annotations

import asyncio
import unittest

from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.history import ConversationTurn


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_session_is_serialized(self) -> None:
        manager = SessionManager()
        active = 0
        peak = 0

        async def worker() -> None:
            nonlocal active, peak
            async with manager.lock("same"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(worker(), worker())
        self.assertEqual(peak, 1)

    async def test_different_sessions_can_run_concurrently(self) -> None:
        manager = SessionManager()
        entered = 0
        both_entered = asyncio.Event()

        async def worker(session: str) -> None:
            nonlocal entered
            async with manager.lock(session):
                entered += 1
                if entered == 2:
                    both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=0.5)

        await asyncio.gather(worker("a"), worker("b"))
        self.assertEqual(entered, 2)

    async def test_history_is_session_isolated_and_bounded(self) -> None:
        manager = SessionManager(history_limit=1)
        manager.append("a", ConversationTurn("1", "a", 1.0))
        manager.append("a", ConversationTurn("2", "b", 2.0))
        manager.append("b", ConversationTurn("3", "c", 3.0))
        self.assertEqual([turn.user_text for turn in manager.history("a")], ["2"])
        self.assertEqual([turn.user_text for turn in manager.history("b")], ["3"])


if __name__ == "__main__":
    unittest.main()
