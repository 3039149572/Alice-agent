from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
import subprocess
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.memory.local_sqlite import LocalSQLiteMemoryAdapter, LocalSQLiteMemoryConfig
from astrbot_plugin_alice_agent.adapters.storage.sqlite import SQLiteStorageAdapter
from astrbot_plugin_alice_agent.adapters.storage.delivery_journal import SQLiteDeliveryJournal
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.core.delivery_commit import DeliveryCommitCoordinator
from astrbot_plugin_alice_agent.alice_agent.core.proactive_delivery import ProactiveDeliveryCommitter
from astrbot_plugin_alice_agent.alice_agent.core.pipeline import AgentPipeline
from astrbot_plugin_alice_agent.alice_agent.core.session_manager import SessionManager
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryItem, MemoryObservation, MemoryMetadata
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.memory.data_service import MemoryDataService
from astrbot_plugin_alice_agent.alice_agent.memory.governance import MemoryScope
from astrbot_plugin_alice_agent.alice_agent.memory.manager import MemoryManager
from astrbot_plugin_alice_agent.alice_agent.memory.metadata_store import SQLiteMemoryMetadataStore
from astrbot_plugin_alice_agent.alice_agent.memory.retriever import MemoryRetriever
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.memory.writer import MemoryWriter
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.relationship.manager import RelationshipManager
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState
from astrbot_plugin_alice_agent.alice_agent.relationship.reducer import RelationshipReducer
from astrbot_plugin_alice_agent.alice_agent.relationship.store import SQLiteRelationshipStore
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer
from astrbot_plugin_alice_agent.alice_agent.loop.store import SQLiteAutonomyStore
from astrbot_plugin_alice_agent.alice_agent.proactive.models import CandidateAction, CandidateActionType


class DeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.event = AgentEvent("e", "trace", "private", "test", "s", "u", self.now, "Chinese original source")
        self.perception = PerceptionResult(valence=0.5, importance=0.9, memory_candidate=True, confidence=0.9,
                                           social_signals={"affection": 0.8})

    def build(self, root):
        port = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(root / "memory.db"))
        metadata = SQLiteMemoryMetadataStore(root / "intelligence.db")
        memory = MemoryManager(port, MemoryRetriever(port, 5), MemoryWriter(port, 0.5), MemoryReranker(), 5, metadata_store=metadata)
        states = StateManager(SQLiteStorageAdapter(root / "state.db"), StateReducer())
        relationship = RelationshipManager(SQLiteRelationshipStore(root / "intelligence.db"), RelationshipReducer())
        sessions = SessionManager(storage=SQLiteStorageAdapter(root / "history.db"))
        journal = SQLiteDeliveryJournal(root / "operations.db")
        coordinator = DeliveryCommitCoordinator(journal, states, memory, sessions, relationship)
        async def close():
            await memory.close()
            await states.close()
            await relationship.close()
            await sessions.close()
            await journal.close()
        return coordinator, port, metadata, close

    def response(self, seed):
        return AgentResponse("generated full text beyond delivered prefix", self.event.trace_id, metadata={
            "_alice_pending_commit": {"perception": self.perception, "plan": ActionPlan(),
                "selected_memories": [seed], "state": AgentState(), "relationship": RelationshipState("u")},
            "turn_diagnostics": {},
        })

    async def seed(self, port, metadata):
        seed = MemoryItem("seed", "older item", importance=0.8, created_at=self.now - 1,
                          metadata={"user_id": "u", "session_id": "s", "trace_id": "seed"})
        await port.observe(MemoryObservation(seed, "ok", True))
        await metadata.save(MemoryMetadata("seed", importance=0.8))
        return seed

    async def test_restart_after_each_store_write_replays_without_duplicate_deltas(self):
        for stage in ("state", "relationship", "access", "memory", "history", "receipt"):
            with self.subTest(stage=stage):
                root = self.root / stage
                coordinator, port, metadata, close = self.build(root)
                seed = await self.seed(port, metadata)
                targets = {"state": (coordinator.states, "update_once"), "relationship": (coordinator.relationship, "update_once"),
                           "access": (coordinator.memory, "record_access"), "memory": (coordinator.memory, "observe"),
                           "history": (coordinator.sessions, "append_and_persist"), "receipt": (coordinator.journal, "complete")}
                target, name = targets[stage]
                original = getattr(target, name)
                async def fail_after(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise OSError("process stopped after durable write")
                setattr(target, name, fail_after)
                with self.assertRaises(OSError):
                    await coordinator.commit(self.event, self.response(seed), "delivered prefix")
                await close()
                recovered, port, metadata, close = self.build(root)
                self.addAsyncCleanup(close)
                for operation_id, payload in await recovered.journal.pending():
                    await recovered.apply(operation_id, payload)
                await recovered.commit(self.event, self.response(seed), "delivered prefix")
                expected = StateReducer().reduce(AgentState(), self.perception, self.event)
                self.assertEqual(asdict(await recovered.states.get("s")), asdict(expected))
                self.assertEqual((await recovered.relationship.get("u")).interaction_count, 1)
                self.assertEqual((await metadata.get_many(["seed"]))["seed"].access_count, 1)
                self.assertEqual(await port.counts(), (2, 2))
                history = await recovered.sessions.load_history("s")
                self.assertEqual([turn.assistant_text for turn in history], ["delivered prefix"])
                self.assertEqual(await recovered.journal.pending(), [])
                row = recovered.journal._db.execute("SELECT payload,user_key FROM delivery_commits").fetchone()
                self.assertIsNone(row["payload"])
                self.assertNotEqual(row["user_key"], "u")

    async def test_metadata_failure_after_observation_is_retried_without_duplicate_memory(self):
        coordinator, port, metadata, close = self.build(self.root)
        self.addAsyncCleanup(close)
        seed = await self.seed(port, metadata)
        original = metadata.save
        async def unavailable(_metadata):
            raise OSError("metadata unavailable")
        metadata.save = unavailable
        with self.assertRaises(OSError):
            await coordinator.commit(self.event, self.response(seed), "actual reply")
        self.assertEqual(await port.counts(), (2, 2))
        metadata.save = original
        operation_id, payload = (await coordinator.journal.pending())[0]
        await coordinator.apply(operation_id, payload)
        self.assertEqual(await port.counts(), (2, 2))
        self.assertEqual((await metadata.get_many(["seed"]))["seed"].access_count, 1)

    async def test_same_delivery_id_cannot_change_transcript_and_receipts_have_no_private_text(self):
        coordinator, port, metadata, close = self.build(self.root)
        self.addAsyncCleanup(close)
        seed = await self.seed(port, metadata)
        await coordinator.commit(self.event, self.response(seed), "actual")
        with self.assertRaises(ValueError):
            await coordinator.commit(self.event, self.response(seed), "invented extra words")
        row = coordinator.journal._db.execute("SELECT * FROM delivery_commits").fetchone()
        self.assertNotIn(self.event.text, str(tuple(row)))
        self.assertNotIn("actual", str(tuple(row)))

    async def test_pending_delivery_recovers_before_self_service_clear_and_cannot_rehydrate(self):
        coordinator, port, metadata, close = self.build(self.root)
        self.addAsyncCleanup(close)
        seed = await self.seed(port, metadata)
        service = MemoryDataService(port, metadata)
        pipeline = AgentPipeline(config=AgentConfig(), perception=None, state_manager=coordinator.states, memory=coordinator.memory,
                                 planner=None, response_generator=None, session_manager=coordinator.sessions, logger=NullLogger(),
                                 relationship_manager=coordinator.relationship, delivery_journal=coordinator.journal, data_service=service)
        service.delivery_recovery = pipeline.recover_deliveries
        original = coordinator.journal.complete
        async def fail(_id):
            raise OSError("receipt outage")
        coordinator.journal.complete = fail
        with self.assertRaises(OSError):
            await coordinator.commit(self.event, self.response(seed), "actual")
        coordinator.journal.complete = original
        scope = MemoryScope("u", "s")
        preview = await service.command(scope, "clear-memory")
        self.assertEqual(await coordinator.journal.pending(), [])
        token = preview.rsplit(" ", 1)[-1]
        await service.command(scope, f"confirm {token}")
        self.assertEqual(await port.counts(), (0, 0))
        await pipeline.recover_deliveries()
        self.assertEqual(await port.counts(), (0, 0))
        self.assertEqual((await coordinator.relationship.get("u")).interaction_count, 1)

    async def test_proactive_restart_recovers_history_cooldown_and_action_once(self):
        for stage in ("autonomy", "access", "history"):
            with self.subTest(stage=stage):
                root = self.root / stage
                coordinator, port, metadata, close = self.build(root)
                seed = await self.seed(port, metadata)
                store = SQLiteAutonomyStore(root / "autonomy.db")
                await store.upsert_session("s", "u", self.now)
                committer = ProactiveDeliveryCommitter(coordinator.journal, store, coordinator.memory, coordinator.sessions)
                action = CandidateAction(CandidateActionType.PROACTIVE_MESSAGE, "reason", action_id="action", topic="topic", metadata={"session_id": "s"})
                payload = committer.payload(self.event, action, "confirmed prefix", "confirmed prefix and unsent suffix", [seed], "2026-10-22", "daily", None)
                target, name = {"autonomy": (store, "commit_delivery_once"), "access": (coordinator.memory, "record_access"), "history": (coordinator.sessions, "append_and_persist")}[stage]
                original = getattr(target, name)
                async def fail_after(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise OSError("stopped after proactive write")
                setattr(target, name, fail_after)
                with self.assertRaises(OSError):
                    await committer.commit(payload)
                await close()
                restored, port, metadata, close = self.build(root)
                self.addAsyncCleanup(close)
                store = SQLiteAutonomyStore(root / "autonomy.db")
                committer = ProactiveDeliveryCommitter(restored.journal, store, restored.memory, restored.sessions)
                restored.proactive_handler = committer.apply
                operation_id, pending = (await restored.journal.pending())[0]
                await restored.apply(operation_id, pending)
                await committer.commit(payload)
                stats = await store.send_stats("u", "topic", self.now - 86400, "s")
                self.assertEqual(stats["daily_count"], 1)
                self.assertEqual((await store.get_daily_proactive_state("s", "2026-10-22"))["delivered_event_ids"], ["daily"])
                self.assertEqual((await metadata.get_many(["seed"]))["seed"].access_count, 1)
                self.assertEqual([x.assistant_text for x in await restored.sessions.load_history("s")], ["confirmed prefix"])
                self.assertEqual(await restored.journal.pending(), [])

    async def test_proactive_cancellation_preserves_only_confirmed_prefix(self):
        from astrbot_plugin_alice_agent.adapters.astrbot.proactive_sender import AstrBotProactiveSender
        class Context:
            def __init__(self):
                self.sent = []
            async def send_message(self, session, chain):
                self.sent.append(chain)
                return True
        context = Context()
        entered, release = asyncio.Event(), asyncio.Event()
        saved = []
        async def pause(_seconds):
            entered.set()
            await release.wait()
        async def record(text):
            saved.append(text)
        sender = AstrBotProactiveSender(context, NullLogger(), split_enabled=True, split_max_chars=50, sleep_func=pause)
        text = "甲" * 45 + "。" + "乙" * 45 + "。"
        task = asyncio.create_task(sender.send("s", text, on_delivered=record))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(context.sent), 1)
        self.assertEqual(saved, ["甲" * 45 + "。"])

    async def test_abrupt_process_exit_recovers_committed_wal_without_clean_close(self):
        code = textwrap.dedent('''
            import asyncio, os, sys
            from pathlib import Path
            from astrbot_plugin_alice_agent.tests.test_delivery_recovery import DeliveryRecoveryTests
            from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
            from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
            async def run():
                fixture = object.__new__(DeliveryRecoveryTests)
                fixture.now = float(sys.argv[2])
                fixture.event = AgentEvent("e", "trace", "private", "test", "s", "u", fixture.now, "Chinese original source")
                fixture.perception = PerceptionResult(valence=0.5, importance=0.9, memory_candidate=True, confidence=0.9, social_signals={"affection":0.8})
                coordinator, port, metadata, close = fixture.build(Path(sys.argv[1]))
                seed = await fixture.seed(port, metadata)
                async def exit_before_history(*args, **kwargs):
                    os._exit(73)
                coordinator.sessions.append_and_persist = exit_before_history
                await coordinator.commit(fixture.event, fixture.response(seed), "confirmed before abrupt exit")
            asyncio.run(run())
        ''')
        outcome = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(self.root), str(self.now)],
                                         cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
        self.assertEqual(outcome.returncode, 73, outcome.stderr)
        coordinator, port, metadata, close = self.build(self.root)
        self.addAsyncCleanup(close)
        pending = await coordinator.journal.pending()
        self.assertEqual(len(pending), 1)
        await coordinator.apply(*pending[0])
        self.assertEqual((await coordinator.relationship.get("u")).interaction_count, 1)
        self.assertEqual((await metadata.get_many(["seed"]))["seed"].access_count, 1)
        self.assertEqual(await port.counts(), (2, 2))
        self.assertEqual([x.assistant_text for x in await coordinator.sessions.load_history("s")], ["confirmed before abrupt exit"])
