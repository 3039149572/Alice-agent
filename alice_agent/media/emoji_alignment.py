from __future__ import annotations

import re
from dataclasses import dataclass

from .models import MediaCandidate


_QUOTED_TEXT = re.compile(
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'|`[^`]*`"
)
_LEGACY_CAPTIONS = re.compile(
    r"“([^”]{1,80})”|‘([^’]{1,80})’|「([^」]{1,80})」|『([^』]{1,80})』|"
    r"\"([^\"]{1,80})\"|'([^']{1,80})'"
)
_BARE_CAPTION = re.compile(
    r"(?:配文|配有文字|文字(?:为|是)|写着|写有)\s*([A-Za-z][A-Za-z0-9~!！?？_-]{1,40})",
    re.IGNORECASE,
)
_CAPTION_MARKUP = re.compile(
    r"(?:配文|配有文字|文字(?:气泡|为|是)?|写着|写有|标有)\s*"
    r"[A-Za-z][A-Za-z0-9~!！?？_-]{1,40}"
)
_CAPTION_LABELS = re.compile(
    r"(?:配文|配有文字|文字(?:气泡|为|是)?|写着|写有|标有|上方|下方|画面下方)"
)
_INTENT_NAMES = {
    "greeting", "farewell", "rest", "gratitude", "acknowledgment", "celebration",
    "praise", "humor", "comfort", "warmth", "crying", "mockery", "romance",
    "anger", "rejection", "confusion", "fatigue", "shy", "goodnight", "birthday",
    "nap", "early_class", "uncertainty",
}
_INTENT_ALIASES = {
    "hello": "greeting", "greet": "greeting", "goodbye": "farewell", "bye": "farewell",
    "sleep": "rest", "thanks": "gratitude", "thank_you": "gratitude",
    "ack": "acknowledgment", "ok": "acknowledgment", "cheering": "celebration",
    "celebrate": "celebration", "congratulations": "celebration", "happy": "celebration",
    "approval": "praise", "funny": "humor", "empathy": "comfort", "support": "comfort",
    "soft_smile": "warmth", "warm": "warmth", "cry": "crying", "tears": "crying",
    "sarcasm": "mockery", "taunt": "mockery", "romantic": "romance", "love": "romance",
    "confused": "confusion", "tired": "fatigue", "embarrassed": "shy",
}

_CAPTION_INTENTS = {
    "拜拜": ("farewell",), "再见": ("farewell",), "回头见": ("farewell",), "明天见": ("farewell",),
    "goodbye": ("farewell",), "bye": ("farewell",), "seeyou": ("farewell",), "またね": ("farewell",),
    "晚安": ("goodnight",), "goodnight": ("goodnight",), "おやすみ": ("goodnight",),
    "你好": ("greeting",), "嗨": ("greeting",), "哈喽": ("greeting",), "早安": ("greeting",),
    "早上好": ("greeting",), "早早早": ("greeting",), "hello": ("greeting",), "hi": ("greeting",),
    "ciallo": ("greeting",), "こんにちは": ("greeting",), "おはよう": ("greeting",),
    "谢谢": ("gratitude",), "感谢": ("gratitude",), "多谢": ("gratitude",), "thankyou": ("gratitude",),
    "收到": ("acknowledgment",), "嗯嗯": ("acknowledgment",), "好好好": ("acknowledgment",),
    "ok": ("acknowledgment",), "okay": ("acknowledgment",), "没问题": ("acknowledgment",),
    "恭喜": ("celebration", "praise"), "生日快乐": ("birthday",), "厉害": ("praise",),
    "真棒": ("praise",), "干得漂亮": ("praise",), "点赞": ("praise",), "好有趣": ("humor",),
    "有意思": ("humor",), "哈哈哈": ("humor",), "晚点聊": ("farewell",), "睡了": ("rest",),
    "午睡": ("nap",), "我上早八": ("early_class",), "我不知道": ("uncertainty",),
    "加油": ("comfort",), "抱抱": ("comfort", "warmth"),
    "开心": ("celebration",), "哭": ("crying",), "大哭": ("crying",), "泪目": ("crying",),
    "笨蛋": ("mockery",), "菜": ("mockery",), "呵呵": ("mockery",), "滚": ("rejection",),
    "给我爬": ("rejection",), "翻脸": ("anger",), "生气": ("anger",), "爱心": ("romance",),
    "比心": ("romance",), "爱你": ("romance",), "心碎": ("romance",), "害羞": ("shy",),
    "迷茫": ("confusion",), "疑惑": ("confusion",), "懵逼": ("confusion",), "摆烂": ("fatigue",),
    "累了": ("fatigue",),
}

_CANDIDATE_RULES: dict[str, tuple[str, ...]] = {
    "crying": (r"哭喊|大哭|哭泣|哭得|流泪|泪如泉涌|泪流满面|眼泪|泪水|哭丧",),
    "mockery": (r"嘲讽|嘲笑|讽刺|挖苦|贬低|骂人|笨蛋|不屑|倒赞|羞辱|奚落",),
    "romance": (r"传递爱意|爱心|比心|亲吻|恋爱|示爱|心碎",),
    "anger": (r"怒目|咬牙切齿|愤怒|生气|怒吼|翻脸|暴怒",),
    "rejection": (r"拒绝|驱赶|赶走|不听|左右摆动.*?拒绝",),
    "farewell": (r"挥手告别|告别|挥手离开|挥手送别",),
    "greeting": (r"打招呼|挥手.*?打招呼",),
    "rest": (r"午睡|熟睡|安睡|睡眠|睡觉|睡了|趴着休息|枕手安睡|盖好被子|鼻涕泡|zzz",),
    "gratitude": (r"感激|感谢|道谢|表达谢意",),
    "acknowledgment": (r"点头|敬礼|OK手势|比出OK|红色对勾|确认收到",),
    "celebration": (r"欢呼|庆祝|大笑|开心大笑|生日蛋糕|彩带|气球|荧光棒|欢快",),
    "praise": (r"竖起大拇指|竖拇指|点赞|夸赞|赞许|鼓掌|表扬",),
    "humor": (r"憋笑|忍笑|偷笑|大笑|哈哈|逗趣",),
    "comfort": (r"安慰|加油|鼓励|安抚|拥抱|抱抱|安慰姿态",),
    "warmth": (r"温柔微笑|温和微笑|面带微笑|微笑|闭眼微笑|温柔地笑|轻轻点头",),
    "confusion": (r"困惑|疑惑|迷茫|茫然|不解|问号|挠头|不知道|装傻",),
    "fatigue": (r"疲惫|疲倦|累了|无奈|摆烂|放弃的姿态|无神",),
    "shy": (r"害羞|脸颊泛红.*?闪躲|目光闪躲",),
}

_RESPONSE_RULES: dict[str, tuple[str, ...]] = {
    "greeting": (r"你好|嗨|哈喽|早安|早上好|早早早|good\s*morning|hello|\bhi\b|こんにちは|おはよう",),
    "farewell": (r"拜拜|再见|回头见|明天见|明天再聊|下次聊|先下了|我先走了|有空再聊|good\s*bye|goodbye|\bbye\b|see\s+you|またね|さようなら",),
    "goodnight": (r"晚安|good\s*night|おやすみ",),
    "rest": (r"去睡觉|准备睡|我要睡|先睡了|睡觉了|睡了",),
    "birthday": (r"生日快乐|happy\s+birthday",),
    "nap": (r"午睡|睡午觉|\bnap\b",),
    "early_class": (r"早八|早上有课|一大早上课",),
    "gratitude": (r"谢谢|感谢|多谢|感激|thank\s+you|thanks|grateful|ありがとう",),
    "acknowledgment": (r"收到|嗯嗯|好好好|没问题|我明白了|明白了|知道了|懂了|\bokay\b|\bok\b|roger",),
    "celebration": (r"恭喜|祝贺|太棒|真棒|做到了|成功了|考上了|通过了|终于解决|太好了|干得漂亮|congrats|well\s+done|yay",),
    "praise": (r"你真厉害|真厉害|你做得很好|做得漂亮|干得漂亮|为你骄傲|厉害了|nice\s+work",),
    "humor": (r"笑死|哈哈+|太好笑|好有趣|真有意思|有意思|funny|hilarious|\blol\b|\bhaha+\b",),
    "comfort": (r"抱抱|抱一下|陪着你|陪你|我在这里|我在这儿|辛苦了|别担心|不用担心|没关系|慢慢来|别难过|别哭|别怕|不用怕|一切会好起来|你已经很努力",),
    "warmth": (r"谢谢你|我很珍惜|心疼你|我关心你|我在乎你|替你开心|真心替你高兴|抱抱|陪着你|陪你|辛苦了|为你骄傲|没关系|我在这里",),
    "crying": (r"我哭了|我想哭|我快哭了|真的要哭了|我看哭了|哭死了|感动哭|泪目|泪流满面|\bi(?:'m| am) crying\b|\bsobbing\b",),
    "mockery": (r"你真笨|笨蛋|你太菜|真菜|废物|stupid|idiot|loser",),
    "romance": (r"我爱你|爱你|喜欢你|想你|亲爱的|宝贝|吻你|love\s+you|miss\s+you",),
    "anger": (r"我很生气|我生气了|气死我了|我真的很愤怒|我火大",),
    "rejection": (r"别再联系|不要联系|滚开|离我远点|我拒绝|no\s+way",),
    "confusion": (r"我没看懂|我不太明白|我没明白|我有点困惑|是什么意思|能再解释一下吗|which\s+one\??",),
    "fatigue": (r"我累了|我好累|累坏了|困死了|我想休息|累得不行",),
    "shy": (r"我有点害羞|不好意思|我害羞了",),
    "uncertainty": (r"我不知道|我不确定|我没把握",),
}

_NEGATION_PREFIX = re.compile(
    r"(?:不|没|别|不要|勿|未|无|并非|不是|不想|不必|不用|无需|不再|从未|从不)"
    r"[\w\u3400-\u9fff]{0,6}$|"
    r"\b(?:not|never|no|don't|dont|do not|didn't|did not|won't|will not)"
    r"(?:\s+\w+){0,3}\s+$",
    re.IGNORECASE,
)
_MENTION_PREFIX = re.compile(
    r"(?:你说的|你提到的|刚才说的|用户说的|提到的|引用的)$|"
    r"(?:例如|比如|譬如|举例|示例)[\w\u3400-\u9fff，,：:\s]{0,8}$"
)
_SENSITIVE_INTENTS = frozenset({"crying", "mockery", "romance", "anger", "rejection"})


@dataclass(frozen=True, slots=True)
class EmojiEligibility:
    eligible: bool
    reason: str
    intents: tuple[str, ...]


def _normalize_label(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _canonical_intent(value: str) -> str | None:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    canonical = _INTENT_ALIASES.get(normalized, normalized)
    return canonical if canonical in _INTENT_NAMES else None


def _legacy_caption_values(description: str) -> tuple[str, ...]:
    values: list[str] = []
    for groups in _LEGACY_CAPTIONS.findall(description):
        value = next((part for part in groups if part), "").strip()
        if value:
            values.append(value)
    if not values:
        match = _BARE_CAPTION.search(description)
        if match:
            values.append(match.group(1).strip())
    return tuple(dict.fromkeys(values))


def response_text_for_alignment(text: str) -> str:
    """Remove quoted/inline-literal spans before the conservative reply rules run."""
    unquoted = re.sub(r"```.*?```|~~~.*?~~~", " ", text or "", flags=re.DOTALL)
    unquoted = re.sub(r"(?m)^\s*>.*$", " ", unquoted)
    unquoted = re.sub(r"`[^`\n]*`", " ", unquoted)
    unquoted = _QUOTED_TEXT.sub(" ", unquoted)
    return re.sub(r"\s+", " ", unquoted).strip()


def _has_active_cue(text: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            prefix = text[max(0, match.start() - 18):match.start()]
            if _MENTION_PREFIX.search(prefix) or _NEGATION_PREFIX.search(prefix):
                continue
            return True
    return False


def response_intents(text: str) -> tuple[str, ...]:
    clean = response_text_for_alignment(text)
    found = {
        intent for intent, patterns in _RESPONSE_RULES.items()
        if _has_active_cue(clean, patterns)
    }
    # A mixed positive and negative answer is ambiguous for decorative media.
    positive = {"greeting", "gratitude", "acknowledgment", "celebration", "praise", "romance"}
    negative = {"crying", "mockery", "anger", "rejection", "fatigue"}
    if found & positive and found & negative:
        found.difference_update(positive | negative)
    # Mocking is only eligible when the assistant explicitly frames the line as play.
    if "mockery" in found and not re.search(
        r"开玩笑|逗你玩|闹着玩|打趣|别当真|just\s+kidding|kidding|teasing",
        clean,
        re.IGNORECASE,
    ):
        found.remove("mockery")
    return tuple(sorted(found))


def candidate_intents(candidate: MediaCandidate) -> tuple[str, ...]:
    description = candidate.description or ""
    visual = _QUOTED_TEXT.sub(" ", description)
    visual = _CAPTION_MARKUP.sub(" ", visual)
    visual = _CAPTION_LABELS.sub(" ", visual)
    visual = re.sub(r"\s+", " ", visual)

    found: set[str] = set()
    if candidate.intent:
        canonical = _canonical_intent(candidate.intent)
        if canonical:
            found.add(canonical)
    for tag in candidate.tags:
        canonical = _canonical_intent(str(tag))
        if canonical:
            found.add(canonical)
    for intent, patterns in _CANDIDATE_RULES.items():
        if any(re.search(pattern, visual, re.IGNORECASE) for pattern in patterns):
            found.add(intent)

    captions = _candidate_captions(candidate)
    for value in captions:
        normalized = _normalize_label(value)
        found.update(_CAPTION_INTENTS.get(normalized, ()))
    return tuple(sorted(found))


def _candidate_captions(candidate: MediaCandidate) -> tuple[str, ...]:
    values = list(_legacy_caption_values(candidate.description or ""))
    if candidate.caption:
        values.extend(part.strip() for part in candidate.caption.split("/") if part.strip())
    return tuple(dict.fromkeys(value for value in values if value))


def _caption_is_standalone(value: str, text: str) -> bool:
    normalized = _normalize_label(value)
    segments = re.split(r"[,，。！？!?;；\n]+", text)
    return bool(normalized) and any(_normalize_label(part.strip()) == normalized for part in segments)


def evaluate_emoji(
    candidate: MediaCandidate,
    reply_intents: tuple[str, ...],
    reply_text: str = "",
) -> EmojiEligibility:
    intents = candidate_intents(candidate)
    replies = set(reply_intents)

    captions = _candidate_captions(candidate)
    caption_match = False
    clean_reply = response_text_for_alignment(reply_text)
    for caption in captions:
        caption_intents = set(_CAPTION_INTENTS.get(_normalize_label(caption), ()))
        if caption_intents:
            if not caption_intents & replies:
                return EmojiEligibility(False, "caption_intent_unmatched", intents)
        elif _caption_is_standalone(caption, clean_reply):
            caption_match = True
        else:
            return EmojiEligibility(False, "caption_unmatched", intents)

    if not reply_intents and not caption_match:
        return EmojiEligibility(False, "no_reply_intent", intents)
    if not intents and not caption_match:
        return EmojiEligibility(False, "no_candidate_intent", intents)

    sensitive = set(intents) & _SENSITIVE_INTENTS
    unmatched_sensitive = sensitive - replies
    if unmatched_sensitive:
        return EmojiEligibility(False, "sensitive_intent_unmatched", intents)

    salient = set(intents) - {"warmth"}
    compatible_salient = salient & replies
    if "rest" in salient and replies & {"goodnight", "rest", "nap"}:
        compatible_salient.add("rest")
    # Generic smiling is a fallback only when the image has no other salient
    # action or message. It cannot turn a sleep, farewell, shy, or taunting
    # image into a fit for an otherwise warm reply.
    if compatible_salient:
        return EmojiEligibility(True, "intent_match", intents)
    if not salient and "warmth" in intents and replies & {"comfort", "warmth", "gratitude", "praise"}:
        return EmojiEligibility(True, "intent_match", intents)
    if caption_match and not salient:
        return EmojiEligibility(True, "caption_literal_match", intents)
    return EmojiEligibility(False, "unrelated_intent", intents)
