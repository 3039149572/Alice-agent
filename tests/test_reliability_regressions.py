from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx

from astrbot_plugin_alice_agent.adapters.memory.local_sqlite import (
    LocalSQLiteMemoryAdapter, LocalSQLiteMemoryConfig,
)
from astrbot_plugin_alice_agent.adapters.astrbot.media_renderer import AstrBotMediaRenderer
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.memory import MemoryItem, MemoryObservation, MemoryQuery
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.media.models import MediaConfig, MediaDecision, MediaCandidate
from astrbot_plugin_alice_agent.alice_agent.memory.reranker import MemoryReranker
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger


class MemoryRecallRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(Path(self.temp.name) / "memory.db"))
        self.addAsyncCleanup(self.adapter.close)

    def query(self, text, topic="general", user="u"):
        return MemoryQuery("s", user, text, topic, PerceptionResult(), AgentState())

    async def add(self, memory_id, text, created, topic="general", user="u"):
        item = MemoryItem(
            id=memory_id, content=text, importance=0.9, created_at=float(created),
            metadata={"user_id": user, "session_id": "s", "topic": topic, "trace_id": memory_id},
        )
        await self.adapter.observe(MemoryObservation(item, None, True))

    async def test_generic_topic_cannot_admit_unrelated_memory(self):
        await self.add("allergy", "我对花生过敏", 1)
        query = self.query("周末去哪里爬山")
        recalled = await self.adapter.recall(query)
        self.assertEqual(recalled, [])
        unscored = await self.adapter.get("allergy")
        self.assertEqual(MemoryReranker().rerank([unscored], 5, query=query).selected, [])

    async def test_exact_old_memory_survives_more_than_300_new_records(self):
        await self.add("allergy", "我对花生过敏", 1)
        for index in range(305):
            await self.add(f"new_{index}", "今天散步看风景", index + 2, "outdoors")
        await self.add("other_user", "我对花生过敏", 999, user="other")
        recalled = await self.adapter.recall(self.query("我对花生过敏"))
        self.assertEqual([item.id for item in recalled], ["allergy"])

    async def test_legacy_index_backfill_and_edit_preserve_data(self):
        await self.add("allergy", "我对花生过敏", 1)
        await self.adapter.close()
        path = Path(self.temp.name) / "memory.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                "DROP TABLE memory_search_terms; DROP TABLE memory_search_documents; PRAGMA user_version=1;"
            )
        self.adapter = LocalSQLiteMemoryAdapter(LocalSQLiteMemoryConfig(path))
        self.addAsyncCleanup(self.adapter.close)
        backups = list(path.parent.glob("memory.db.pre-v3-*.bak"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(backup.execute("SELECT content FROM memories").fetchone()[0], "我对花生过敏")
        self.assertEqual(await self.adapter.counts(), (1, 1))
        self.assertEqual([item.id for item in await self.adapter.recall(self.query("我对花生过敏"))], ["allergy"])
        item = await self.adapter.get("allergy")
        item.content = "我最喜欢蓝色"
        item.metadata["topic"] = "颜色偏好"
        await self.adapter.update(item)
        self.assertEqual(await self.adapter.recall(self.query("我对花生过敏")), [])
        self.assertEqual([item.id for item in await self.adapter.recall(self.query("我最喜欢蓝色"))], ["allergy"])


class MediaDeliveryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_render_keeps_reference_and_pending_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            refs = {name: root / f"reference_{name}.wav" for name in ("A", "B")}
            for path in refs.values():
                path.write_bytes(b"RIFF")
            waiting = asyncio.Event()
            resume = asyncio.Event()
            current_ref = None
            synthesis = {}

            class TranslationModel:
                async def generate(self, request):
                    if request.session_id == "A":
                        waiting.set()
                        await resume.wait()
                    return LLMResult(text=f"translation_{request.session_id}")

            async def handler(request):
                nonlocal current_ref
                body = json.loads(request.content)
                if request.url.path == "/set_reference_audio":
                    current_ref = Path(body["audio_path"]).name
                if request.url.path == "/tts":
                    synthesis[body["text"]] = current_ref
                    return httpx.Response(200, content=b"\x00\x00" * 64)
                return httpx.Response(200, json={"ok": True})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                renderer = AstrBotMediaRenderer(
                    MediaConfig(enabled=True, character_name="alice", onnx_model_dir="model"),
                    TranslationModel(), NullLogger(), root / "audio", client=client,
                )
                def decision(name):
                    return MediaDecision(send_voice=True, voice=MediaCandidate(
                        name, "audio", str(refs[name]), reference_text=name,
                    ))
                task = asyncio.create_task(renderer.render("A", "message_A", decision("A")))
                try:
                    await asyncio.wait_for(waiting.wait(), 2)
                    result_b = await renderer.render("B", "message_B", decision("B"))
                    resume.set()
                    result_a = await task
                    self.assertEqual(synthesis["translation_A"], refs["A"].name)
                    self.assertEqual(synthesis["translation_B"], refs["B"].name)
                    self.assertTrue(Path(result_a.audio_path).is_file())
                    self.assertTrue(Path(result_b.audio_path).is_file())
                finally:
                    resume.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await renderer.close()

    async def test_voice_only_history_contains_only_spoken_source(self):
        from astrbot_plugin_alice_agent.main import AliceAgentPlugin
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "ref.wav"
            reference.write_bytes(b"RIFF")
            spoken, committed = [], []
            async def handler(request):
                if request.url.path == "/tts":
                    spoken.append(json.loads(request.content)["text"])
                    return httpx.Response(200, content=b"\x00\x00" * 64)
                return httpx.Response(200, json={"ok": True})
            async def commit(event, response, text):
                committed.append(text)
            async def noop(*args, **kwargs):
                pass
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                config = MediaConfig(
                    enabled=True, character_name="alice", onnx_model_dir="model", language="chinese",
                    default_ref_audio=str(reference), default_ref_text="reference", send_text_with_audio=False,
                )
                renderer = AstrBotMediaRenderer(config, None, NullLogger(), root / "audio", client=client)
                plugin = AliceAgentPlugin.__new__(AliceAgentPlugin)
                plugin._is_batch_current = lambda *args: True
                plugin._media_renderer, plugin._media_config, plugin._config = renderer, config, AgentConfig()
                plugin._segment_settings = lambda: (True, 200, 0.0)
                plugin._agent = SimpleNamespace(commit_delivery=commit, record_suppressed=noop)
                plugin._record_test_turn = noop
                framework = SimpleNamespace(message_obj=None, chain_result=lambda chain: chain,
                                            plain_result=lambda text: text, stop_event=lambda: None)
                event = SimpleNamespace(session_id="s", source="aiocqhttp", metadata={})
                response = AgentResponse(text="A" * 100 + "\n" + "B" * 50, trace_id="audit",
                                         metadata={"media_decision": MediaDecision(send_voice=True)})
                delivery = {"yielded_text_segments": [], "segments_yielded": 0}
                async for _ in plugin._deliver_response_locked(
                    framework, event, response, SimpleNamespace(phase="ready"), 1, 1, delivery, {},
                ):
                    pass
                self.assertEqual(committed, spoken)
                self.assertEqual(len(committed[0]), 80)
                self.assertEqual(delivery["yielded_text_segments"], [])
                await renderer.close()
