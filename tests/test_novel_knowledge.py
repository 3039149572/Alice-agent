from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_alice_agent import main as plugin_main
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.daily_events.service import DailyEventService
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.history import ConversationTurn
from astrbot_plugin_alice_agent.alice_agent.domain.llm import LLMResult
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.generation.response_generator import ResponseGenerator
from astrbot_plugin_alice_agent.alice_agent.knowledge import NovelContextConfig, NovelKnowledge
from astrbot_plugin_alice_agent.alice_agent.knowledge.novel import has_childhood_recollection
from astrbot_plugin_alice_agent.alice_agent.persona import profile as profiles
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState
from astrbot_plugin_alice_agent.scripts.import_novel_context import build, convert
from astrbot_plugin_alice_agent.tests.test_astrbot_adapter import FakeContext, FakeEvent


DATA = Path(__file__).resolve().parents[1] / "alice_agent" / "knowledge" / "data"


class NovelRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.knowledge = NovelKnowledge(DATA)

    def test_packaged_corpus_is_loaded_and_personas_are_separate(self) -> None:
        manifest = json.loads((DATA / "source_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(self.knowledge.counts["facts"], manifest["counts"]["facts.jsonl"]["included"])
        self.assertEqual(self.knowledge.counts["dialogues"], manifest["counts"]["dialogues.jsonl"]["included"])
        self.assertTrue(all(c["persona_origin"] == "knight" for c in self.knowledge._dialogues))
        childhood = [c for c in self.knowledge._facts if c["persona_origin"] == "childhood"]
        self.assertTrue(childhood)
        self.assertTrue(all(c["knowledge_mode"] == "reader_background" for c in childhood))

    def test_ice_cave_childhood_is_recalled_only_as_reader_background(self) -> None:
        context = self.knowledge.retrieve("你小时候和尤吉欧一起去过冰洞吗？")
        self.assertTrue(context["facts"])
        first = context["facts"][0]
        self.assertIn("冰洞", first["title"])
        self.assertEqual(first["persona_origin"], "childhood")
        self.assertEqual(first["knowledge_mode"], "reader_background")
        self.assertIn("不能编造转述者", first["usage_rule"])
        self.assertIn("不拥有童年", context["memory_boundary"])
        self.assertFalse(context["dialogue_examples"])

    def test_hearing_about_sister_does_not_restore_childhood_memory(self) -> None:
        context = self.knowledge.retrieve("桐人告诉你赛尔卡是妹妹，你能想起她吗？")
        card = next(c for c in context["facts"] if c["id"] == "full-h075440-b001-f02")
        self.assertIn("仍是背景或转述", card["usage_rule"])
        self.assertIn("不等于恢复童年记忆", card["usage_rule"])
        self.assertTrue(all(c["persona_origin"] == "knight" for c in context["dialogue_examples"]))

    def test_specific_duty_question_prefers_the_actual_knight_position(self) -> None:
        context = self.knowledge.retrieve("你为什么反抗公理教会？")
        self.assertIn("守护人界人民", context["facts"][0]["title"])
        example = next(c for c in context["dialogue_examples"] if c["id"] == "full-h085074-b001-d01")
        self.assertIn("我的使命，绝非维护公理教会", example["quote"])
        self.assertEqual(context["facts"][0]["knowledge_mode"], "self_experience")

    def test_real_world_press_conference_is_not_backfilled_into_default_stage(self) -> None:
        query = "你在现实世界记者会上说过什么？"
        default = self.knowledge.retrieve(query)
        self.assertEqual(default["facts"], [])
        self.assertEqual(default["dialogue_examples"], [])
        later = NovelKnowledge(DATA, NovelContextConfig(stage="later_canon")).retrieve(query)
        self.assertTrue(later["facts"] or later["dialogue_examples"])
        self.assertTrue(any(c["stage_scope"] == "later_canon" for c in later["facts"] + later["dialogue_examples"]))
        self.assertIn("不拥有童年", later["memory_boundary"])

    def test_daily_fatigue_does_not_fetch_plot_or_combat_and_intimacy(self) -> None:
        context = self.knowledge.retrieve("今天有点累了，我不想再勉强自己。", plan=ActionPlan(action="comfort"))
        self.assertEqual(context["facts"], [])
        self.assertEqual(context["dialogue_examples"], [])

    def test_generic_tags_cannot_trigger_examples_but_specific_scene_can(self) -> None:
        for query in ("关心一下我", "请解释这个", "给我帮助", "日常聊天", "帮我分析Python代码"):
            result = self.knowledge.retrieve(query, plan=ActionPlan(action="comfort"))
            self.assertEqual(result["dialogue_examples"], [], query)
        specific = self.knowledge.retrieve("资源不足了，目前上限是多少？")
        self.assertEqual([c["id"] for c in specific["dialogue_examples"]], ["st-006"])
        self.assertNotIn("tags", specific["dialogue_examples"][0])
        self.assertIn("资源不足", specific["dialogue_examples"][0]["matched_terms"])
        self.assertEqual(self.knowledge.retrieve("具体是哪位？")["dialogue_examples"], [])
        clarifying = self.knowledge.retrieve("具体是哪位？", plan=ActionPlan(action="clarify"))
        self.assertEqual([c["id"] for c in clarifying["dialogue_examples"]], ["st-013"])
        self.assertNotIn("full-h073844-b002-d02", {c["id"] for c in self.knowledge._dialogues})

    def test_greeting_does_not_fake_reunion_and_unknown_input_abstains(self) -> None:
        self.assertEqual(self.knowledge.retrieve("你好，爱丽丝")["dialogue_examples"], [])
        for query in ("", "qzxv789 unrelated", "123456789"):
            result = self.knowledge.retrieve(query)
            self.assertEqual(result["facts"], [])
            self.assertEqual(result["dialogue_examples"], [])

    def test_teasing_requires_established_familiarity(self) -> None:
        plan = ActionPlan(action="tease")
        text = "骑士也会肚子饿吗？"
        stranger = self.knowledge.retrieve(text, plan=plan)
        self.assertEqual(stranger["dialogue_examples"], [])
        familiar = self.knowledge.retrieve(text, plan=plan,
            relationship=RelationshipState("u", familiarity=0.6, interaction_count=8))
        self.assertEqual([c["id"] for c in familiar["dialogue_examples"]], ["full-h075440-b001-d01"])

    def test_context_budget_preserves_complete_json_and_caps_lists(self) -> None:
        for budget in (1000, 2000, 6000):
            knowledge = NovelKnowledge(DATA, NovelContextConfig(max_chars=budget, fact_limit=4, dialogue_limit=4))
            result = knowledge.retrieve("你为什么反抗公理教会，桐人告诉你童年的记忆了吗？")
            serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
            self.assertLessEqual(len(serialized), budget)
            self.assertEqual(json.loads(serialized), result)
            self.assertLessEqual(len(result["facts"]), 4)
            self.assertLessEqual(len(result["dialogue_examples"]), 4)

    def test_missing_disabled_or_corrupt_data_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(NovelKnowledge(root).retrieve("童年")["facts"], [])
            child = next(c for c in self.knowledge._facts if c["persona_origin"] == "childhood")
            bad_child = {**child, "knowledge_mode": "self_experience"}
            valid = self.knowledge._facts[0]
            (root / "facts.jsonl").write_text("not-json\n" + json.dumps(bad_child) + "\n" + json.dumps(valid) + "\n", encoding="utf-8")
            broad_example = {**self.knowledge._dialogues[0], "match_terms": ["关心"]}
            (root / "dialogues.jsonl").write_text("[]\n{}\n" + json.dumps(broad_example), encoding="utf-8")
            self.assertEqual(NovelKnowledge(root).counts, {"facts": 1, "dialogues": 0})
            self.assertEqual(NovelKnowledge(root, NovelContextConfig(enabled=False)).counts, {"facts": 0, "dialogues": 0})

    def test_queries_do_not_mutate_shared_corpus_or_remember_users(self) -> None:
        before = copy.deepcopy(self.knowledge._facts)
        self.knowledge.retrieve("用户甲秘密-unique-private-text，童年怎么了")
        second = self.knowledge.retrieve("用户乙今天累了", plan=ActionPlan(action="comfort"))
        self.assertEqual(self.knowledge._facts, before)
        self.assertNotIn("unique-private-text", json.dumps(second, ensure_ascii=False))
        self.assertNotIn("用户甲", json.dumps(second, ensure_ascii=False))

    def test_frozen_bercouli_is_not_mislabeled_as_dead(self) -> None:
        card = next(c for c in self.knowledge._facts if c["id"] == "full-h075440-b004-f01")
        self.assertNotIn("临终", card["title"])
        self.assertIn("不是其后来战死事件", card["limitations"])


class NovelPromptAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_response_request_carries_examples_and_separate_user_memory(self) -> None:
        class CapturingPort:
            requests = []

            async def generate(self, request):
                self.requests.append(request)
                return LLMResult("我想守护的是人界的人民。")

        port = CapturingPort()
        builder = ContextBuilder()
        generator = ResponseGenerator(port, builder)
        event = AgentEvent("e", "trace", "private", "test", "session", "user", 1, "你为什么反抗公理教会？")
        response = await generator.generate(event, PerceptionResult(), AgentState(), [], ActionPlan(), [])
        self.assertEqual(len(port.requests), 1)
        payload = json.loads(port.requests[0].prompt)
        self.assertTrue(payload["character_knowledge"]["facts"])
        self.assertTrue(payload["character_knowledge"]["dialogue_examples"])
        self.assertEqual(payload["selected_memories"], [])
        self.assertIn("没有小时候的亲历记忆", port.requests[0].system_prompt)
        self.assertIn("不把当前用户自动当成桐人", payload["persona"])
        self.assertEqual(response.text, "我想守护的是人界的人民。")

    async def test_handler_wires_config_and_retrieval_without_extra_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(plugin_main.StarTools, "get_data_dir", return_value=Path(temporary)):
            context = FakeContext()
            plugin = plugin_main.AliceAgentPlugin(context, {
                "vad_enabled": False, "memory_backend": "in_memory", "memory_enabled": False,
                "session_persistence_enabled": False, "response_timing_enabled": False,
                "segmented_send_enabled": False, "agent_loop_enabled": False,
                "novel_dialogue_limit": 1, "novel_fact_limit": 1,
            })
            try:
                event = FakeEvent("你为什么反抗公理教会？")
                results = [x async for x in plugin.on_private_message(event)]
                self.assertTrue(results)
                self.assertTrue(event.stopped)
                self.assertEqual(len(context.llm_calls), 3)  # perception, planner, response
                prompt = json.loads(context.llm_calls[-1]["prompt"])
                self.assertEqual(len(prompt["character_knowledge"]["facts"]), 1)
                self.assertEqual(len(prompt["character_knowledge"]["dialogue_examples"]), 1)
                self.assertEqual(prompt["selected_memories"], [])
            finally:
                await plugin.terminate()

    async def test_user_report_and_old_false_history_cannot_change_static_memory_rule(self) -> None:
        builder = ContextBuilder()
        event = AgentEvent("e", "t", "private", "test", "s", "u", 2, "我听说你小时候去过冰洞，这是你的回忆吗？")
        history = [ConversationTurn("还记得小时候吗", "我记得童年一起去过那里。", 1)]
        system, prompt = builder.for_response(event, PerceptionResult(), AgentState(), [], ActionPlan(), history)
        payload = json.loads(prompt)
        self.assertIn("童年内容只能", payload["character_knowledge"]["memory_boundary"])
        self.assertIn("当前用户的转述也不自动成为原著事实", system)
        self.assertTrue(all(c["knowledge_mode"] == "reader_background"
                            for c in payload["character_knowledge"]["facts"] if c["persona_origin"] == "childhood"))
        self.assertEqual(payload["recent_history"][0]["assistant_text"], history[0].assistant_text)

    async def test_daily_events_reject_childhood_flashback_and_default_profile_migrates(self) -> None:
        fallback = "Alice回想了一点剑术上的问题。"
        self.assertEqual(DailyEventService._safe_text("Alice想起小时候和尤吉欧在冰洞里的经历。", fallback, "reflection"), fallback)
        self.assertEqual(DailyEventService._safe_text("Alice回想了一点剑术上的问题。", fallback, "reflection"), fallback)
        legacy = {"name": "Alice", "life_background": profiles._LEGACY_DEFAULT_BACKGROUND,
                  "interests": list(profiles._LEGACY_DEFAULT_INTERESTS),
                  "daily_rhythm": list(profiles._LEGACY_DEFAULT_RHYTHM), "daily_event_seeds": {}}
        migrated = profiles.PersonaProfile.from_value(legacy)
        self.assertIn("整合骑士", migrated.life_background)
        custom = profiles.PersonaProfile.from_value({**legacy, "interests": ["园艺"]})
        self.assertEqual(custom.interests, ("园艺",))
        self.assertIn("普通生活", custom.life_background)
        childhood_profile = profiles.PersonaProfile.from_value({
            "interests": ["童年记忆"], "daily_event_seeds": {
                "reflection": ["Alice回忆起小时候与尤吉欧的冒险。"],
            },
        })
        self.assertNotIn("reflection", childhood_profile.daily_event_seeds)
        seed, _ = childhood_profile.event_seed("reflection", random.Random(0))
        self.assertFalse(has_childhood_recollection(seed))

    async def test_config_bounds_disable_and_planner_do_not_mix_novel_ids_with_memory(self) -> None:
        config = AgentConfig(novel_context_stage="unknown", novel_fact_limit=-1,
                             novel_dialogue_limit=99, novel_context_max_chars=float("inf"))
        self.assertEqual(config.novel_context_stage, "integrity_knight")
        self.assertEqual(config.novel_fact_limit, 0)
        self.assertEqual(config.novel_dialogue_limit, 4)
        self.assertEqual(config.novel_context_max_chars, 6000)
        builder = ContextBuilder(novel_config=NovelContextConfig(enabled=False))
        event = AgentEvent("e", "t", "private", "test", "s", "u", 1, "你小时候和尤吉欧一起去过冰洞吗？")
        system, prompt = builder.for_planner(event, PerceptionResult(), AgentState(), [], [])
        payload = json.loads(prompt)
        self.assertEqual(payload["character_knowledge"]["facts"], [])
        self.assertNotIn("dialogue_examples", payload["character_knowledge"])
        self.assertIn("不能放进 memory_ids", system)


class CorpusImportTests(unittest.TestCase):
    def test_user_archive_conversion_demotes_childhood_and_never_extracts_extra_members(self) -> None:
        sample = {"id": "child-1", "title": "冰洞", "summary": "童年爱丽丝走进冰洞。",
                  "persona_origin": "childhood", "stage": "第九卷", "source_family": "main",
                  "knowledge_mode": "self_experience", "default_retrieval": False,
                  "source": {"sha256": "0" * 64}, "evidence": [{"line": 1, "quote": "冰洞"}]}
        converted = convert(sample, dialogue=False)
        self.assertEqual(converted["knowledge_mode"], "reader_background")
        self.assertEqual(converted["stage_scope"], "childhood")
        self.assertIsNone(convert({**sample, "persona_origin": "other_or_uncertain"}, dialogue=False))
        broad = {**sample, "id": "broad-example", "persona_origin": "knight",
                 "default_retrieval": True, "tag_analysis": {"certainty": "explicit"}}
        self.assertIsNone(convert(broad, dialogue=True))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "pack.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("alice_context_pack_full/03_人物对话示例.jsonl", "")
                zipped.writestr("alice_context_pack_full/04_剧情与关系资料.jsonl", json.dumps(sample))
                zipped.writestr("alice_context_pack_full/资料来源与验证.json", json.dumps({"source_sha256": "0" * 64, "not_verified": []}))
                zipped.writestr("../../do_not_extract.py", "raise RuntimeError('must not run')")
            manifest = build(archive, root / "out")
            self.assertEqual(manifest["counts"]["facts.jsonl"]["included"], 1)
            self.assertEqual(sorted(p.name for p in (root / "out").iterdir()), ["dialogues.jsonl", "facts.jsonl", "source_manifest.json"])
            self.assertFalse((root / "do_not_extract.py").exists())


if __name__ == "__main__":
    unittest.main()
