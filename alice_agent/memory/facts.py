from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FactClaim:
    key: str
    value: str
    evidence: str


_UNSAFE = re.compile(r"[?？\"“”‘’]|如果|假如|假设|要是|可能|也许|好像|以前|曾经|过去|小说|角色|扮演|比如|例如|听说|朋友说|梦见|以后|打算|准备|希望|想要|的话")
_RULES = (
    ("preferred_name", re.compile(r"(?:^|[,，])\s*我(?:的名字是|叫)([^,，。！!；;\n]{1,30})$")),
    ("home_city", re.compile(r"(?:^|[,，])\s*我(?:现在|目前)?(?:住在|居住在)([^,，。！!；;\n]{1,40})$")),
    ("occupation", re.compile(r"(?:^|[,，])\s*我的(?:职业|工作)是([^,，。！!；;\n]{1,40})$")),
    ("favorite_color", re.compile(r"(?:^|[,，])\s*我(?:现在|目前)?最喜欢的颜色是([^,，。！!；;\n]{1,20})$")),
    ("preferred_language", re.compile(r"(?:^|[,，])\s*我(?:现在)?(?:偏好|常用|使用)的语言是([^,，。！!；;\n]{1,20})$")),
)


def _value(text: str) -> str:
    return text.strip().removesuffix("了").strip()


def extract_explicit_facts(text: str) -> list[FactClaim]:
    """Conservative current-user assertions only; uncertainty means abstention.

    No model call, invented paraphrase, quoted speech, historical fact, future
    plan or roleplay content can create a profile fact through this path.
    """
    result: dict[str, FactClaim] = {}
    if _UNSAFE.search(text):
        return []
    for clause in re.split(r"[。！!；;\n]+", text[:8000]):
        clause = clause.strip()
        if not clause:
            continue
        for key, rule in _RULES:
            if match := rule.search(clause):
                value = _value(match[1])
                if value and not re.search(r"不是|不再|并非|而是|但是|不过|以及|和", value):
                    result[key] = FactClaim(key, value, clause)
        allergy = re.fullmatch(r"我(?:现在|已经|目前)?对([^,，。！？\n]{1,24}?)(不再|不)?过敏(?:了)?", clause)
        if allergy:
            entity = allergy[1].strip()
            if entity and not re.search(r"什么|任何|所有|哪些|或者|还是|和|及", entity):
                result["allergy/" + entity.casefold()] = FactClaim(
                    "allergy/" + entity.casefold(), "no" if allergy[2] else "yes", clause,
                )
        preference = re.fullmatch(r"我(?:现在|目前)?(不喜欢|喜欢)([^,，。！？\n]{1,30})", clause)
        if preference:
            entity = _value(preference[2])
            if entity and not re.search(r"什么|任何|所有|哪些|或者|还是|和|及|你|他|她", entity):
                result["preference/" + entity.casefold()] = FactClaim(
                    "preference/" + entity.casefold(), "dislike" if preference[1] == "不喜欢" else "like", clause,
                )
    return list(result.values())[:3]


def fact_query_keys(text: str) -> set[str]:
    """Specific first-person profile queries, rather than generic topic tags."""
    result = set()
    for key, pattern in (
        ("home_city", r"我(?:现在|目前)?住(?:哪|在)|我的(?:住处|住址|居住地)"),
        ("preferred_name", r"我叫什么|我的名字|怎么称呼我|如何称呼我"),
        ("occupation", r"我的(?:职业|工作)|我做什么工作|我是做什么的"),
        ("favorite_color", r"我.*(?:喜欢|偏好).*颜色|我的.*颜色偏好"),
        ("preferred_language", r"我.*(?:偏好|常用|使用).*语言"),
        ("allergy", r"我.*(?:对什么|对哪些|有无|过敏史|过敏情况).*过敏|我的过敏史"),
        ("preference", r"我(?:现在|最)?喜欢什么|我的喜好"),
    ):
        if re.search(pattern, text):
            result.add(key)
    if match := re.search(r"我对([^,，。！？?\n]{1,24}?)(?:不再|不)?过敏", text):
        entity = match[1].strip()
        if not re.search(r"什么|哪些|任何|所有", entity):
            result.add("allergy/" + entity.casefold())
    return result
