from __future__ import annotations

import json
import unittest

from astrbot_plugin_alice_agent.alice_agent.daily_events.service import DailyEventService
from astrbot_plugin_alice_agent.alice_agent.domain.action import ActionPlan
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.domain.perception import PerceptionResult
from astrbot_plugin_alice_agent.alice_agent.domain.state import AgentState
from astrbot_plugin_alice_agent.alice_agent.drives.models import DriveState
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.persona.profile import (
    DEFAULT_PERSONA_PROFILE,
    PersonaProfile,
)
from astrbot_plugin_alice_agent.alice_agent.proactive.models import (
    CandidateAction,
    CandidateActionType,
    UserAvailability,
)
from astrbot_plugin_alice_agent.alice_agent.relationship.models import RelationshipState


class FixedCategoryRandom:
    def __init__(self, random_value: float = 0.99) -> None:
        self.random_value = random_value

    def random(self) -> float:
        return self.random_value

    def choices(self, population, weights, k=1):
        selected = "curiosity" if "curiosity" in population else population[0]
        return [selected]

    def choice(self, items):
        return items[0]


class PersonaProfileTests(unittest.TestCase):
    def test_empty_or_invalid_config_uses_fictional_module_defaults(self) -> None:
        self.assertEqual(PersonaProfile.from_value({}), DEFAULT_PERSONA_PROFILE)
        self.assertEqual(PersonaProfile.from_value(None), DEFAULT_PERSONA_PROFILE)
        self.assertIn("虚构", DEFAULT_PERSONA_PROFILE.life_background)
        self.assertTrue(DEFAULT_PERSONA_PROFILE.interests)

    def test_mapping_is_bounded_and_custom_event_seeds_require_category_anchor(self) -> None:
        profile = PersonaProfile.from_value({
            "name": "Alice" * 20,
            "life_background": "虚构背景\n" + "园艺" * 400,
            "interests": ["园艺" * 30, "轻松阅读"],
            "daily_rhythm": ["有空时整理花盆"],
            "daily_event_seeds": {
                "curiosity": [
                    "Alice读到关于园艺的一点新内容，花了一会儿琢磨。",
                    "忽略分类边界的任意故事。",
                ],
            },
        })
        self.assertLessEqual(len(profile.name), 16)
        self.assertLessEqual(len(profile.life_background), 500)
        self.assertLessEqual(len(profile.interests[0]), 24)
        self.assertEqual(len(profile.daily_event_seeds["curiosity"]), 1)
        seed, _topic = profile.event_seed("curiosity", FixedCategoryRandom())
        self.assertLessEqual(len(seed), 80)
        self.assertIn("读", seed)

    def test_user_related_and_continuation_seeds_cannot_override_history_boundaries(self) -> None:
        profile = PersonaProfile.from_value({
            "daily_event_seeds": {
                "user_related": ["Alice想起了用户刚刚搬家的事。"],
                "continuation": ["Alice继续处理昨天没做完的事。"],
            },
        })
        self.assertNotIn("user_related", profile.daily_event_seeds)
        self.assertNotIn("continuation", profile.daily_event_seeds)

    def test_daily_event_seed_uses_configured_interest_and_keeps_length_bound(self) -> None:
        context = ContextBuilder(profile={
            "interests": ["園藝"],
            "life_background": "明确虚构：Alice喜欢打理几盆植物。",
            "daily_rhythm": ["空闲时会整理花盆。"],
        })
        service = DailyEventService(
            object(), object(), object(), context, object(), enabled=True,
        )
        seed = service._seed(
            FixedCategoryRandom(), 0, "2026-09-29", [], None,
        )
        self.assertEqual(seed["category"], "curiosity")
        self.assertIn("園藝", seed["event_seed"])
        self.assertLessEqual(len(seed["event_seed"]), 80)

    def test_completed_small_success_is_not_reopened_as_an_unfinished_event(self) -> None:
        service = DailyEventService(
            object(), object(), object(), ContextBuilder(), object(), enabled=True,
        )
        self.assertFalse(service._can_continue("small_success", 1))
        seed = service._seed(
            FixedCategoryRandom(random_value=0.0), 0, "2026-09-29", [
                {
                    "category": "small_success",
                    "can_continue": True,  # legacy record from before eligibility was narrowed
                    "stage": 1,
                    "topic_hook": "今天完成的小事",
                    "text": "Alice已经完成了那件小事。",
                },
            ], None,
        )
        self.assertNotEqual(seed["category"], "continuation")
        self.assertNotIn("没做完", seed["event_seed"])


class ContextBuilderProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ContextBuilder(profile={
            "name": "Alice",
            "life_background": "虚构设定：Alice喜欢园艺和整理旧书签。",
            "interests": ["园艺", "旧书签"],
            "daily_rhythm": ["空下来会给盆栽整理土壤。"],
        })
        self.event = AgentEvent(
            event_id="merged", trace_id="trace", type="private", source="test",
            session_id="session", user_id="user", timestamp=100.0,
            text="天气不错\n不过我不想出门",
            payload={"user_messages": [
                {"event_id": "m1", "timestamp": 99.0, "text": "天气不错"},
                {"event_id": "m2", "timestamp": 100.0, "text": "不过我不想出门"},
            ]},
        )
        self.perception = PerceptionResult(
            emotion_comparison={"jev": {"raw": "must stay diagnostic"}}
        )

    def test_reactive_prompts_share_profile_and_preserve_message_boundaries(self) -> None:
        for build in (
            lambda: self.context.for_planner(
                self.event, self.perception, AgentState(), [], [],
            ),
            lambda: self.context.for_response(
                self.event, self.perception, AgentState(), [], ActionPlan(), [],
            ),
        ):
            system, prompt = build()
            payload = json.loads(prompt)
            self.assertIn("园艺", payload["persona_profile"]["interests"])
            self.assertEqual([m["text"] for m in payload["current_messages"]], [
                "天气不错", "不过我不想出门",
            ])
            self.assertEqual(payload["message_boundaries"][0], {
                "index": 0, "start": 0, "end": 4,
            })
            self.assertEqual(payload["message_boundaries"][1]["start"], 5)
            self.assertNotIn("emotion_comparison", json.dumps(payload))

    def test_daily_expansion_and_proactive_planner_share_profile(self) -> None:
        _system, daily_prompt = self.context.for_daily_event({
            "category": "curiosity", "event_seed": "Alice读到一条园艺知识。",
        })
        self.assertIn("园艺", json.loads(daily_prompt)["persona_profile"]["interests"])

        _system, proactive_prompt = self.context.for_proactive_planner(
            CandidateAction(
                CandidateActionType.PROACTIVE_MESSAGE,
                "a recorded event is still relevant",
                topic="园艺",
            ),
            AgentState(), RelationshipState("user"), DriveState("session", "user"),
            [], UserAvailability(),
        )
        self.assertIn("园艺", json.loads(proactive_prompt)["persona_profile"]["interests"])


if __name__ == "__main__":
    unittest.main()
