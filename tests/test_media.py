from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from astrbot_plugin_alice_agent.adapters.astrbot.media_renderer import AstrBotMediaRenderer
from astrbot_plugin_alice_agent.alice_agent.decision.models import (
    DecisionAnswer,
    DecisionMode,
    DecisionResult,
)
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.media import (
    DecisionAwareMediaSelector,
    MediaCandidate,
    MediaCatalog,
    MediaConfig,
    MediaDecision,
)


class FixedDecisionModel:
    def __init__(self, choices: dict[str, str], nouls: dict[str, float]) -> None:
        self.choices = choices
        self.nouls = nouls
        self.last_state = None
        self.calls = 0

    async def decide(self, state, questions, **kwargs):
        self.calls += 1
        self.last_state = state
        answers = {}
        for key, question in questions.items():
            if question.type == "choice":
                selected = self.choices.get(key, next(iter(question.criteria)))
                answers[key] = DecisionAnswer(
                    "choice",
                    choice=selected,
                    probabilities={item: 0.99 if item == selected else 0.01 for item in question.criteria},
                    confidence=0.99,
                )
            else:
                answers[key] = DecisionAnswer("noul", noul=self.nouls.get(key, 0.0))
        return DecisionResult(
            "media", kwargs["trace_id"], kwargs["decision_type"], kwargs["schema_version"],
            "", "mock-jev", 1.0, answers=answers,
        )

    async def close(self):
        return None


def sample_event() -> AgentEvent:
    return AgentEvent(
        "event", "trace", "USER_MESSAGE", "test", "session", "user", 1.0,
        "今天终于把难题解决了，太开心了！",
    )


class MediaCatalogTests(unittest.TestCase):
    def test_legacy_absolute_path_is_relocated_to_configured_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            audio = audio_dir / "sample.wav"
            audio.write_bytes(b"RIFF")
            index = root / "audio_index.json"
            index.write_text(json.dumps([{
                "path": r"C:\\old\\plugin\\audio\\sample.wav",
                "text": "参考文本",
                "desc": "开心地庆祝",
                "vad": [0.9, 0.8, 0.7],
            }]), encoding="utf-8")
            catalog = MediaCatalog.from_files(
                audio_index_path=str(index), audio_dir=str(audio_dir)
            )
            self.assertEqual(len(catalog.audio), 1)
            self.assertEqual(Path(catalog.audio[0].path), audio.resolve())


class MediaSelectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.catalog = MediaCatalog(
            audio=[
                MediaCandidate("audio_0", "audio", "happy.wav", "开心庆祝", "うれしい", (0.9, 0.8, 0.7), 0.8),
                MediaCandidate("audio_1", "audio", "sad.wav", "低落难过", "かなしい", (0.1, 0.2, 0.3), 0.2),
            ],
            emoji=[
                MediaCandidate("emoji_0", "emoji", "smile.png", "开心地欢呼", vad=(0.9, 0.8, 0.7), energy=0.8),
                MediaCandidate("emoji_1", "emoji", "quiet.png", "平静地微笑", vad=(0.7, 0.2, 0.5), energy=0.2),
            ],
        )
        self.response = AgentResponse("太好了，终于解决了！", "trace")
        self.perception = PerceptionResult(valence=0.9, arousal=0.8, confidence=0.9)
        self.state = AgentState(mood_valence=0.7, mood_arousal=0.7, energy=0.8)
        self.plan = ActionPlan(emotion="happy", energy=0.8, directness=0.7, tone="warm")

    async def test_deterministic_policy_uses_ranked_candidates_when_jev_is_off(self) -> None:
        selector = DecisionAwareMediaSelector(
            self.catalog,
            DecisionService(None, mode=DecisionMode.OFF),
            MediaConfig(enabled=True, audio_score_threshold=0.0, emoji_score_threshold=0.0),
        )
        decision = await selector.decide(
            sample_event(), self.response, self.perception, self.state, self.plan
        )
        self.assertTrue(decision.send_voice)
        self.assertTrue(decision.send_emoji)
        self.assertEqual(decision.voice.id, "audio_0")
        self.assertEqual(decision.emoji.id, "emoji_0")

    async def test_high_confidence_jev_can_suppress_voice_and_choose_bounded_emoji(self) -> None:
        model = FixedDecisionModel(
            {"voice_choice": "audio_0", "emoji_choice": "emoji_1"},
            {"send_voice": 0.1, "send_emoji": 0.99, "requires_deep_reasoning": 0.0},
        )
        service = DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0)
        selector = DecisionAwareMediaSelector(
            self.catalog,
            service,
            MediaConfig(enabled=True, audio_score_threshold=0.0, emoji_score_threshold=0.0),
        )
        decision = await selector.decide(
            sample_event(),
            AgentResponse("谢谢你帮我解决了，真的非常感谢。", "trace"),
            self.perception,
            self.state,
            self.plan,
        )
        self.assertFalse(decision.send_voice)
        self.assertTrue(decision.send_emoji)
        self.assertEqual(decision.emoji.id, "emoji_1")
        self.assertEqual(decision.source, "jev")
        self.assertNotIn("happy.wav", json.dumps(model.last_state, ensure_ascii=False))
        self.assertNotIn("smile.png", json.dumps(model.last_state, ensure_ascii=False))


class FakeLLM:
    async def generate(self, request):
        raise AssertionError("Chinese TTS should not invoke translation")


class RecordingLLM:
    def __init__(self) -> None:
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return LLMResult("模型译文")


class FakeLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        pass


class MediaRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_astrbot_tts_input_log_survives_synthesis_failure(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tts":
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            reference.write_bytes(b"RIFF")
            logger = FakeLogger()
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            renderer = AstrBotMediaRenderer(
                MediaConfig(
                    enabled=True, character_name="alice", onnx_model_dir="model",
                    language="chinese", default_ref_audio=str(reference),
                    default_ref_text="参考文本",
                ),
                FakeLLM(), logger, root / "temp", client=client,
            )
            try:
                rendered = await renderer.render(
                    "session", "合成失败也应保留输入。", MediaDecision(send_voice=True),
                )
                self.assertIsNone(rendered.audio_path)
                entry = next(item for item in logger.infos if item.startswith("Alice media: TTS inputs "))
                logged = json.loads(entry.removeprefix("Alice media: TTS inputs "))
                self.assertEqual(logged["reference_audio"], str(reference))
                self.assertEqual(logged["source_text"], "合成失败也应保留输入。")
                self.assertEqual(logged["translated_text"], "合成失败也应保留输入。")
            finally:
                await renderer.close()
                await client.aclose()

    async def test_astrbot_log_records_actual_reference_and_translated_tts_text(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/translate":
                return httpx.Response(200, json={"translations": [{"text": "こんにちは。"}]})
            if request.url.path == "/tts":
                return httpx.Response(200, content=b"\x00\x00" * 64)
            return httpx.Response(200, json={"ok": True})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_ref = root / "default.wav"
            selected_ref = root / "selected.wav"
            default_ref.write_bytes(b"RIFF")
            selected_ref.write_bytes(b"RIFF")
            logger = FakeLogger()
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            renderer = AstrBotMediaRenderer(
                MediaConfig(
                    enabled=True, character_name="alice", onnx_model_dir="model",
                    language="japanese", deepl_api_key="secret:fx",
                    default_ref_audio=str(default_ref), default_ref_text="默认参考文本",
                ),
                RecordingLLM(), logger, root / "temp", client=client,
            )
            try:
                rendered = await renderer.render(
                    "session", "你好，今天很开心。",
                    MediaDecision(
                        send_voice=True,
                        voice=MediaCandidate(
                            id="selected", kind="audio", path=str(selected_ref),
                            reference_text="选中参考文本",
                        ),
                    ),
                )
                self.assertIsNotNone(rendered.audio_path)
                entry = next(item for item in logger.infos if item.startswith("Alice media: TTS inputs "))
                logged = json.loads(entry.removeprefix("Alice media: TTS inputs "))
                self.assertEqual(logged, {
                    "reference_audio": str(selected_ref),
                    "reference_text": "选中参考文本",
                    "source_text": "你好，今天很开心。",
                    "translated_text": "こんにちは。",
                })
                self.assertNotIn("secret:fx", entry)
            finally:
                await renderer.close()
                await client.aclose()

    async def test_deepl_official_api_is_preferred_for_free_and_pro_keys(self) -> None:
        for key, host, language, target in (
            ("free-key:fx", "api-free.deepl.com", "japanese", "JA"),
            ("pro-key", "api.deepl.com", "english", "EN"),
        ):
            calls = []

            async def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                return httpx.Response(200, json={"translations": [{"text": "  官方译文  "}]})

            with tempfile.TemporaryDirectory() as temp:
                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                llm = RecordingLLM()
                config = MediaConfig(language=language, deepl_api_key=key)
                self.assertNotIn(key, repr(config))
                renderer = AstrBotMediaRenderer(config, llm, FakeLogger(), Path(temp), client=client)
                self.assertEqual(await renderer._translate("session", "你好"), "官方译文")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].url.host, host)
                self.assertEqual(calls[0].url.path, "/v2/translate")
                self.assertEqual(calls[0].headers["Authorization"], f"DeepL-Auth-Key {key}")
                self.assertEqual(json.loads(calls[0].content), {"text": ["你好"], "target_lang": target})
                self.assertEqual(llm.requests, [])
                await client.aclose()

    async def test_deepl_failure_or_invalid_result_falls_back_to_session_model(self) -> None:
        for response in (
            httpx.Response(456, json={"message": "quota exceeded"}),
            httpx.Response(200, json={"translations": []}),
            httpx.Response(200, json={"translations": [{"text": " "}]}),
        ):
            with tempfile.TemporaryDirectory() as temp:
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
                llm = RecordingLLM()
                renderer = AstrBotMediaRenderer(
                    MediaConfig(language="japanese", deepl_api_key="free-key:fx"),
                    llm, FakeLogger(), Path(temp), client=client,
                )
                self.assertEqual(await renderer._translate("session", "你好"), "模型译文")
                self.assertEqual(llm.requests[0].session_id, "session")
                self.assertEqual(llm.requests[0].stage, "media_translation")
                await client.aclose()

    async def test_no_deepl_key_uses_session_model_without_http_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: self.fail("DeepL request without API key")
            ))
            llm = RecordingLLM()
            renderer = AstrBotMediaRenderer(MediaConfig(language="english"), llm, FakeLogger(), Path(temp), client=client)
            self.assertEqual(await renderer._translate("session", "你好"), "模型译文")
            self.assertEqual(len(llm.requests), 1)
            await client.aclose()

    async def test_genie_protocol_produces_wav_without_breaking_text_path(self) -> None:
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/tts":
                return httpx.Response(200, content=b"\x00\x00" * 64)
            return httpx.Response(200, json={"ok": True})

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / "reference.wav"
            reference.write_bytes(b"RIFF")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            config = MediaConfig(
                enabled=True,
                character_name="alice",
                onnx_model_dir="model",
                language="chinese",
                default_ref_audio=str(reference),
                default_ref_text="参考文本",
            )
            renderer = AstrBotMediaRenderer(config, FakeLLM(), FakeLogger(), root / "temp", client=client)
            await renderer.initialize()
            rendered = await renderer.render(
                "session", "这是一条用于语音合成的回复。",
                MediaDecision(send_voice=True, reason="test"),
            )
            self.assertIsNotNone(rendered.audio_path)
            self.assertTrue(Path(rendered.audio_path).read_bytes().startswith(b"RIFF"))
            self.assertIn("/load_character", calls)
            self.assertIn("/set_reference_audio", calls)
            self.assertIn("/tts", calls)
            await renderer.close()
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
