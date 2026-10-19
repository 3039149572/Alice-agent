from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..knowledge.novel import has_childhood_recollection


_DEFAULT_NAME = "Alice"
_LEGACY_DEFAULT_BACKGROUND = (
    "这是明确虚构的普通生活设定，不对应现实中的位置、日程或传感器状态："
    "Alice住在一个不指明城市和季节的普通居住环境里，有一张常用的小书桌，"
    "用来读写、整理便签和放手边的小物件。她的生活以普通琐事和安静兴趣为主，"
    "日子大多平稳，偶尔被小事打断后会重新安排。"
)
_LEGACY_DEFAULT_INTERESTS = (
    "短篇阅读",
    "音乐",
    "简单点心",
    "随手记下的问题",
    "整理小物件",
)
_LEGACY_DEFAULT_RHYTHM = (
    "开始一天时通常先处理一件轻量小事，再逐渐进入状态。",
    "忙碌时会留一点短暂休息；空下来时偏好读几页或听点音乐。",
    "日程偶尔会被琐事打断，她一般通过重新排序来调整。",
)
_DEFAULT_BACKGROUND = (
    "这是《刀剑神域》的虚构角色设定：Alice是整合骑士爱丽丝·辛赛西斯·萨提，"
    "以守护人界和同伴为职责，能独立判断教会命令是否正当。她在合成秘仪后没有童年"
    "爱丽丝·滋贝鲁库的亲历记忆；关于卢利特村和家人的过去只能通过他人讲述了解，"
    "不将熟悉感、资料或转述当成恢复记忆。具体往事按本轮资料注明的阶段和知情方式使用。"
    "这不代表实时行踪、现实身体或与用户已经发生过共同经历。"
)
_DEFAULT_INTERESTS = ("剑术与神圣术的研习", "同伴的近况", "人界与故乡", "职责之外的日常")
_DEFAULT_RHYTHM = (
    "认真处理职责，也允许疲惫、停歇和日常闲谈；不必每轮谈及骑士身份。",
    "没有已记录事件时，不声称正在巡逻、训练或刚见过原著人物。",
    "回顾的是骑士时期的判断和经历，不是已经失去的童年画面。",
)
_DEFAULT_BOUNDARIES = (
    "这些是虚构角色设定，不是现实观测。不得把虚构的作息执行、天气、位置或外部行为"
    "说成真实观测；如系统上下文明确提供了日期或时间，可以据实使用。"
)

_CATEGORY_TOPICS = {
    "routine": "今天整理的小事",
    "curiosity": "今天读到的问题",
    "small_success": "今天完成的小事",
    "small_setback": "今天的小插曲",
    "reflection": "今天想到的旧事",
    "social": "今天的一次交谈",
    "user_related": "之前聊过的事",
    "continuation": "昨天未完的小事",
    "rare": "偶然翻到的旧笔记",
}

_UNSAFE_WORDS = ("死亡", "杀人", "自杀", "重伤", "毁灭世界", "世界末日")
_CATEGORY_ANCHORS = {
    "routine": ("整理", "安排"),
    "curiosity": ("读", "问题", "琢磨"),
    "small_success": ("完成", "做好", "解决"),
    "small_setback": ("打乱", "不顺", "耽搁", "调整"),
    "reflection": ("想起", "回想", "回忆"),
    "social": ("交谈", "聊天", "说话"),
    "user_related": ("想起", "记起", "在意"),
    "continuation": ("继续", "昨天", "接着"),
    "rare": ("发现", "翻到", "偶然"),
}


def _clean_text(value: object, *, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    cleaned = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return cleaned[:limit].strip() or default


def _clean_list(value: object, *, limit: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value[:limit]:
        cleaned = _clean_text(item, limit=item_limit)
        if cleaned and not any(word in cleaned for word in _UNSAFE_WORDS):
            result.append(cleaned)
    return tuple(result)


def _clean_event_seeds(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for category, anchors in _CATEGORY_ANCHORS.items():
        # These two categories require trusted conversation/event history and
        # must not be manufactured by a free-form persona seed.
        if category in {"user_related", "continuation"}:
            continue
        raw = value.get(category)
        if not isinstance(raw, (list, tuple)):
            continue
        seeds: list[str] = []
        for item in raw[:5]:
            text = _clean_text(item, limit=80)
            if (
                4 <= len(text) <= 80
                and not any(word in text for word in _UNSAFE_WORDS)
                and any(anchor in text for anchor in anchors)
                and not has_childhood_recollection(text)
            ):
                seeds.append(text)
        if seeds:
            result[category] = tuple(seeds)
    return result


@dataclass(frozen=True)
class PersonaProfile:
    """Configurable fictional background shared by prompts and daily events."""

    name: str = _DEFAULT_NAME
    life_background: str = _DEFAULT_BACKGROUND
    interests: tuple[str, ...] = _DEFAULT_INTERESTS
    daily_rhythm: tuple[str, ...] = _DEFAULT_RHYTHM
    daily_event_seeds: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: object = None) -> "PersonaProfile":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()

        # AstrBot persists schema defaults as explicit values. Migrate only an
        # exact old default, leaving deliberate custom profiles intact.
        if (
            value.get("name", _DEFAULT_NAME) == _DEFAULT_NAME
            and value.get("life_background") == _LEGACY_DEFAULT_BACKGROUND
            and isinstance(value.get("interests"), (list, tuple))
            and isinstance(value.get("daily_rhythm"), (list, tuple))
            and tuple(value.get("interests", ())) == _LEGACY_DEFAULT_INTERESTS
            and tuple(value.get("daily_rhythm", ())) == _LEGACY_DEFAULT_RHYTHM
            and not value.get("daily_event_seeds")
        ):
            return cls()

        name = _clean_text(value.get("name"), limit=16, default=_DEFAULT_NAME)
        background = _clean_text(
            value.get("life_background"), limit=500, default=_DEFAULT_BACKGROUND
        )
        interests = _clean_list(value.get("interests"), limit=8, item_limit=24)
        rhythm = _clean_list(value.get("daily_rhythm"), limit=6, item_limit=100)
        return cls(
            name=name,
            life_background=background,
            interests=interests or _DEFAULT_INTERESTS,
            daily_rhythm=rhythm or _DEFAULT_RHYTHM,
            daily_event_seeds=_clean_event_seeds(value.get("daily_event_seeds")),
        )

    def prompt_context(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "life_background": self.life_background,
            "interests": list(self.interests),
            "daily_rhythm": list(self.daily_rhythm),
            "boundaries": _DEFAULT_BOUNDARIES,
        }

    def to_config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "life_background": self.life_background,
            "interests": list(self.interests),
            "daily_rhythm": list(self.daily_rhythm),
            "daily_event_seeds": {
                category: list(seeds)
                for category, seeds in self.daily_event_seeds.items()
            },
        }

    def event_seed(self, category: str, rng: random.Random) -> tuple[str, str]:
        custom = self.daily_event_seeds.get(category, ())
        if custom:
            text = rng.choice(custom).replace("Alice", self.name)
            if len(text) <= 80:
                return text, _CATEGORY_TOPICS.get(category, "今天的一件小事")

        interest = rng.choice(self.interests) if self.interests else "手边的小兴趣"
        name = self.name
        seeds = {
            "routine": f"{name}整理了围绕{interest}的小笔记，让今天的安排重新变清楚。",
            "curiosity": f"{name}读到关于{interest}的一点新内容，花了一会儿琢磨。",
            "small_success": f"{name}完成了一件与{interest}有关的小事，心里稍微轻松了些。",
            "small_setback": f"{name}原本留给{interest}的时间被琐事挤占，只好重新调整安排。",
            "reflection": f"{name}想起了与{interest}有关的一件小事，又把注意力放回今天。",
            "social": f"{name}和熟人简短交谈了几句，觉得这一天多了点变化。",
            "user_related": f"{name}想起之前与你说过的一件事，但没有擅自猜测后来如何。",
            "continuation": f"{name}沿着昨天的线索又想了一会儿，给今天记下一个新念头。",
            "rare": f"{name}偶然翻到与{interest}有关的一页旧笔记，停下来读了几行。",
        }
        text = seeds.get(category, f"{name}处理了一件普通小事，然后继续安排今天。")
        if has_childhood_recollection(text):
            text = f"{name}回想今天的一点思考，把注意力重新放回眼前。"
        return text, _CATEGORY_TOPICS.get(
            category, "今天的一件小事"
        )


DEFAULT_PERSONA_PROFILE = PersonaProfile()


def coerce_persona_profile(value: object = None) -> PersonaProfile:
    return PersonaProfile.from_value(value)
