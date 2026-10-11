from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.alice_agent.decision.models import DecisionMode
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.response import AgentResponse
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.media import (
    DecisionAwareMediaSelector,
    MediaCandidate,
    MediaCatalog,
    MediaConfig,
)
from astrbot_plugin_alice_agent.alice_agent.media.emoji_alignment import (
    candidate_intents,
    evaluate_emoji,
    response_intents,
    response_text_for_alignment,
)
from .test_media import FixedDecisionModel, sample_event


class RecordingDecisionModel(FixedDecisionModel):
    def __init__(self, choices: dict[str, str], nouls: dict[str, float]) -> None:
        super().__init__(choices, nouls)
        self.last_questions: set[str] = set()

    async def decide(self, state, questions, **kwargs):
        self.last_questions = set(questions)
        return await super().decide(state, questions, **kwargs)


class EmojiAlignmentTests(unittest.TestCase):
    def test_legacy_caption_is_a_constraint_not_a_free_keyword(self) -> None:
        celebration = MediaCandidate(
            "sun", "emoji", "sun.png", "双手高举欢呼，配有文字“赞美太阳”"
        )
        birthday = MediaCandidate(
            "birthday", "emoji", "birthday.png", "捧生日蛋糕开心大笑，配文「生日快乐」"
        )
        success_reply = "太好了，终于把问题解决了！"

        self.assertIn("celebration", candidate_intents(celebration))
        self.assertFalse(evaluate_emoji(celebration, response_intents(success_reply), success_reply).eligible)
        self.assertFalse(evaluate_emoji(birthday, response_intents(success_reply), success_reply).eligible)

    def test_specific_sleep_caption_does_not_match_goodnight(self) -> None:
        nap = MediaCandidate(
            "nap", "emoji", "nap.png", "闭眼趴着休息，面带放松微笑，配文“午睡”"
        )
        goodnight_reply = "晚安早点休息，明天再聊。"

        result = evaluate_emoji(nap, response_intents(goodnight_reply), goodnight_reply)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "caption_intent_unmatched")

    def test_example_mentions_do_not_count_as_active_reply_intent(self) -> None:
        reply = "例如，晚安是睡前常用的问候语。"
        sleeping = MediaCandidate("sleep", "emoji", "sleep.png", "闭眼睡觉")

        self.assertNotIn("goodnight", response_intents(reply))
        self.assertFalse(evaluate_emoji(sleeping, response_intents(reply), reply).eligible)

    def test_farewell_synonyms_match_but_user_only_words_do_not(self) -> None:
        bye = MediaCandidate(
            "bye", "emoji", "bye.png", "微笑着挥手告别，画面下方配有文字『拜拜』"
        )
        reply = "先这样啦，回头见。"
        self.assertTrue(evaluate_emoji(bye, response_intents(reply), reply).eligible)

        quoted_only = "你刚才说的“再见”我看到了，稍后我确认一下。"
        result = evaluate_emoji(bye, response_intents(quoted_only), quoted_only)
        self.assertFalse(result.eligible)

    def test_negated_and_quoted_intents_are_not_positive_evidence(self) -> None:
        goodbye = MediaCandidate(
            "night", "emoji", "night.png", "挥手告别，配有文字“晚安”"
        )
        response = "别说“晚安”了，我们明天再聊。"
        self.assertFalse(evaluate_emoji(goodbye, response_intents(response), response).eligible)
        self.assertFalse(response_intents("不是开心，是难过"))

    def test_blockquotes_and_code_blocks_do_not_supply_reply_intent(self) -> None:
        quoted = "> 谢谢你！\n```python\nprint(谢谢)\n```我会检查配置。"
        self.assertEqual(response_intents(quoted), ())
        self.assertNotIn("谢谢", response_text_for_alignment(quoted))

    def test_empathy_does_not_select_strong_crying_or_mockery(self) -> None:
        empathy = "听起来很难受，我会陪着你慢慢说。"
        crying = MediaCandidate("cry", "emoji", "cry.png", "张大嘴流泪哭喊，泪如泉涌")
        mockery = MediaCandidate("insult", "emoji", "insult.png", "得意地骂人，露出嘲讽微笑")
        for candidate in (crying, mockery):
            result = evaluate_emoji(candidate, response_intents(empathy), empathy)
            self.assertFalse(result.eligible)
            self.assertEqual(result.reason, "sensitive_intent_unmatched")

    def test_romantic_visual_needs_explicit_affection(self) -> None:
        heart = MediaCandidate("heart", "emoji", "heart.png", "双手捧着大红心，传递爱意")
        thanks = "谢谢你帮我找到这个问题。"
        result = evaluate_emoji(heart, response_intents(thanks), thanks)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "sensitive_intent_unmatched")

    def test_warmth_fallback_does_not_override_captionless_salient_visuals(self) -> None:
        thanks = "谢谢你帮我处理这个问题。"
        candidates = (
            MediaCandidate("sleep", "emoji", "sleep.png", "闭眼睡觉，面带微笑"),
            MediaCandidate("shy", "emoji", "脸红害羞，温柔微笑"),
            MediaCandidate("bye", "emoji", "开心地挥手告别，面带微笑"),
        )
        for candidate in candidates:
            self.assertFalse(evaluate_emoji(candidate, response_intents(thanks), thanks).eligible)

        gentle_smile = MediaCandidate("smile", "emoji", "smile.png", "温柔微笑")
        self.assertTrue(evaluate_emoji(gentle_smile, response_intents(thanks), thanks).eligible)

    def test_caption_words_startup_mom_and_early_class_do_not_leak_as_visual_intent(self) -> None:
        startup = MediaCandidate("startup", "emoji", "startup.png", "面带微笑，手捧设备，文字“启动”")
        mom_cry = MediaCandidate("mom", "emoji", "mom.png", "张大嘴流泪哭喊，旁边配有“妈妈”文字")
        early_class = MediaCandidate("early", "emoji", "early.png", "表情疲惫，配文「我上早八」")
        neutral = "修改端口配置后重新启动服务，运行情况正常。"

        for candidate in (startup, mom_cry, early_class):
            self.assertFalse(evaluate_emoji(candidate, response_intents(neutral), neutral).eligible)

    def test_vad_only_candidate_cannot_pass(self) -> None:
        candidate = MediaCandidate("empty", "emoji", "empty.png", vad=(0.9, 0.8, 0.7), energy=0.9)
        thanks = "谢谢你帮我处理好了。"
        result = evaluate_emoji(candidate, response_intents(thanks), thanks)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "no_candidate_intent")

    def test_explicit_metadata_remains_supported(self) -> None:
        candidate = MediaCandidate(
            "tagged", "emoji", "tagged.png", "",
            caption="明天见", intent="farewell", tags=("social",),
        )
        reply = "先下線了，明天见。"
        result = evaluate_emoji(candidate, response_intents(reply), reply)
        self.assertTrue(result.eligible)


class EmojiSelectorAlignmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_text_and_high_vad_do_not_create_emoji_match(self) -> None:
        candidate = MediaCandidate(
            "bye", "emoji", "bye.png", "背对镜头挥手告别，配文“拜拜”",
            vad=(0.9, 0.8, 0.7), energy=0.9,
        )
        selector = DecisionAwareMediaSelector(
            MediaCatalog(emoji=[candidate]),
            DecisionService(None, mode=DecisionMode.OFF),
            MediaConfig(enabled=True, audio_score_threshold=0.0, emoji_score_threshold=0.0),
        )
        event = AgentEvent(
            "event", "trace", "USER_MESSAGE", "test", "session", "user", 1.0,
            "拜拜，我要走了。",
        )
        reply = AgentResponse("修改端口配置后重新启动服务，运行情况正常。", "trace")
        decision = await selector.decide(
            event, reply, PerceptionResult(valence=0.9, arousal=0.8),
            AgentState(mood_valence=0.9, mood_arousal=0.8, energy=0.9),
            ActionPlan(emotion="happy", energy=0.9, tone="warm"),
        )

        self.assertFalse(decision.send_emoji)
        alignment = decision.diagnostics["emoji_alignment"]
        self.assertEqual(alignment["candidate_count"], 1)
        self.assertEqual(alignment["eligible_count"], 0)
        self.assertTrue(alignment["abstained"])

    async def test_legacy_catalog_splits_caption_and_keeps_index_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images"
            image_dir.mkdir()
            image = image_dir / "bye.png"
            image.write_bytes(b"image")
            index = root / "emoji.json"
            index.write_text(
                '[{"path":"C:\\\\old\\\\bye.png","desc":"挥手告别，配文『拜拜』"}]',
                encoding="utf-8",
            )
            catalog = MediaCatalog.from_files(emoji_index_path=str(index), emoji_dir=str(image_dir))

        self.assertEqual(len(catalog.emoji), 1)
        self.assertEqual(catalog.emoji[0].caption, "拜拜")
        self.assertEqual(catalog.emoji[0].path, str(image.resolve()))


class EmojiSelectorHardGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.audio = MediaCandidate(
            "audio_0", "audio", "voice.wav", "通用参考音频", "reference", (0.6, 0.3, 0.5), 0.5
        )
        self.emoji = MediaCandidate("emoji_0", "emoji", "smile.png", "温柔微笑")
        self.response = AgentResponse("谢谢你帮我处理这个问题。", "trace")
        self.perception = PerceptionResult(valence=0.6, arousal=0.3, confidence=0.9)
        self.state = AgentState(mood_valence=0.5, mood_arousal=0.3, energy=0.5)
        self.plan = ActionPlan(emotion="warm", energy=0.5, tone="warm")

    def _selector(self, model, *, mode=DecisionMode.HYBRID, **config):
        media_config = {
            "enabled": True,
            "audio_score_threshold": 0.0,
            "emoji_score_threshold": 0.0,
            **config,
        }
        return DecisionAwareMediaSelector(
            MediaCatalog(audio=[self.audio], emoji=[self.emoji]),
            DecisionService(model, mode=mode, cache_ttl_seconds=0),
            MediaConfig(**media_config),
        )

    async def _decide(self, selector):
        return await selector.decide(
            sample_event(), self.response, self.perception, self.state, self.plan
        )

    async def test_voice_enabled_jev_cannot_bypass_zero_emoji_probability(self) -> None:
        model = RecordingDecisionModel(
            {"voice_choice": "audio_0", "emoji_choice": "emoji_0"},
            {"send_voice": 0.99, "send_emoji": 0.99, "requires_deep_reasoning": 0.0},
        )
        decision = await self._decide(self._selector(model, emoji_probability=0.0))

        self.assertTrue(decision.send_voice)
        self.assertFalse(decision.send_emoji)
        self.assertIn("send_emoji", model.last_questions)

    async def test_score_threshold_removes_emoji_from_jev_choices(self) -> None:
        model = RecordingDecisionModel(
            {"voice_choice": "audio_0", "emoji_choice": "emoji_0"},
            {"send_voice": 0.99, "send_emoji": 0.99, "requires_deep_reasoning": 0.0},
        )
        decision = await self._decide(self._selector(model, emoji_score_threshold=0.99))

        self.assertFalse(decision.send_emoji)
        self.assertNotIn("send_emoji", model.last_questions)
        self.assertEqual(model.last_state["emoji_candidates"], [])

    async def test_shadow_keeps_baseline_decision_and_alignment_diagnostics(self) -> None:
        model = RecordingDecisionModel(
            {"voice_choice": "audio_0", "emoji_choice": "none"},
            {"send_voice": 0.0, "send_emoji": 0.0, "requires_deep_reasoning": 0.0},
        )
        decision = await self._decide(self._selector(model, mode=DecisionMode.SHADOW))

        self.assertEqual(decision.source, "shadow")
        self.assertTrue(decision.send_emoji)
        self.assertEqual(decision.diagnostics["emoji_alignment"]["eligible_count"], 1)

    async def test_no_eligible_emoji_is_not_offered_to_jev(self) -> None:
        model = RecordingDecisionModel(
            {"voice_choice": "audio_0"},
            {"send_voice": 0.99, "requires_deep_reasoning": 0.0},
        )
        selector = DecisionAwareMediaSelector(
            MediaCatalog(audio=[self.audio], emoji=[MediaCandidate(
                "unrelated", "emoji", "bye.png", "挥手告别，配文“拜拜”", vad=(0.9, 0.8, 0.7)
            )]),
            DecisionService(model, mode=DecisionMode.HYBRID, cache_ttl_seconds=0),
            MediaConfig(enabled=True, audio_score_threshold=0.0, emoji_score_threshold=0.0),
        )
        reply = AgentResponse("修改端口配置后重新启动服务，运行情况正常。", "trace")
        decision = await selector.decide(
            sample_event(), reply, self.perception, self.state, self.plan
        )

        self.assertFalse(decision.send_emoji)
        self.assertNotIn("send_emoji", model.last_questions)
        self.assertEqual(model.last_state["emoji_candidates"], [])
        self.assertTrue(decision.diagnostics["emoji_alignment"]["abstained"])


if __name__ == "__main__":
    unittest.main()
