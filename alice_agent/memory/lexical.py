from __future__ import annotations

import re
import unicodedata


_GENERIC_TOPICS = frozenset({
    "", "general", "chat", "unknown", "none", "null", "other", "default",
    "日常", "闲聊", "其他", "未分类", "无", "日常聊天", "普通聊天",
})


def tokens(text: str) -> set[str]:
    lowered = unicodedata.normalize("NFKC", str(text)).lower()
    result = set(re.findall(r"[a-z0-9_]+", lowered))
    for segment in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(segment) == 1:
            result.add(segment)
        else:
            result.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return result


def topic_tokens(topic: str) -> set[str]:
    value = unicodedata.normalize("NFKC", str(topic)).strip().lower()
    if value in _GENERIC_TOPICS:
        return set()
    return tokens(value) - _GENERIC_TOPICS


def overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0
