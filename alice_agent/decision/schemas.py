from __future__ import annotations

from .models import DecisionQuestion


PERCEPTION_SCHEMA = "perception_v2"
MEMORY_SCHEMA = "memory_relevance_v1"
PLANNER_SCHEMA = "planner_v1"
PROACTIVE_SCHEMA = "proactive_v1"
MEDIA_SCHEMA = "media_v1"
DAILY_EVENT_SCHEMA = "daily_event_v1"
RESPONSE_TIMING_SCHEMA = "response_timing_v1"


def response_timing_questions() -> dict[str, DecisionQuestion]:
    return {
        "action": DecisionQuestion(
            "choice",
            "Choose whether Alice should reply now, wait briefly for a likely continuation, or stay silent.",
            {
                "reply_now": "The message is complete, asks a question, requests help, or deserves a timely response.",
                "wait": "The user clearly signals that another message is coming immediately.",
                "ignore": "The user explicitly asks for no reply, or no response would be more respectful.",
            },
        ),
    }


def daily_event_questions() -> dict[str, DecisionQuestion]:
    levels = ["strong decrease", "slight decrease", "no change", "slight increase", "strong increase"]
    return {
        axis: DecisionQuestion(
            "score",
            f"Given Alice's current mood, rate this small event's subjective {axis} shift. "
            "Do not rate the user's emotions.",
            levels,
        )
        for axis in ("valence", "arousal", "dominance", "energy", "stress", "loneliness")
    } | {
        "importance": DecisionQuestion(
            "score", "How personally important is this ordinary event to Alice?",
            ["trivial", "minor", "noticeable", "important", "exceptional"],
        )
    }


def perception_questions() -> dict[str, DecisionQuestion]:
    questions = {
        "intent": DecisionQuestion("choice", "Classify the user's immediate conversational intent.", {
            key: None for key in (
                "greeting", "ask", "inform", "request", "complain", "tease",
                "praise", "comfort", "goodbye", "relationship_signal", "other",
            )
        }),
        "emotion": DecisionQuestion("choice", "Classify the user's expressed emotion.", {
            key: None for key in (
                "positive", "neutral", "negative", "sad", "annoyed", "excited",
                "tired", "anxious", "playful", "uncertain",
            )
        }),
        "vad_valence": DecisionQuestion("score", {
            "question": "Estimate the user's expressed emotional valence on a 0 to 4 scale.",
            "anchors": ["strongly negative", "negative", "neutral or mixed", "positive", "strongly positive"],
        }, ["strongly negative", "negative", "neutral or mixed", "positive", "strongly positive"]),
        "vad_arousal": DecisionQuestion("score", {
            "question": "Estimate emotional activation, from calm to highly activated.",
            "anchors": ["very low", "low", "moderate", "high", "very high"],
        }, ["very low", "low", "moderate", "high", "very high"]),
        "vad_dominance": DecisionQuestion("score", {
            "question": "Estimate the user's expressed sense of control or agency.",
            "anchors": ["very low", "low", "moderate", "high", "very high"],
        }, ["very low", "low", "moderate", "high", "very high"]),
        "emotion_intensity": DecisionQuestion("score", "Rate the expressed emotion intensity.", [
            "none or minimal", "mild", "moderate", "strong", "very strong",
        ]),
        "warmth": DecisionQuestion("score", "Rate the warmth or affiliative tone expressed by the user.", [
            "none or cold", "slight", "moderate", "warm", "very warm",
        ]),
        "tension": DecisionQuestion("score", "Rate the tension or strain expressed by the user.", [
            "none", "slight", "moderate", "high", "very high",
        ]),
        "suppression": DecisionQuestion("score", "Rate how much emotion appears held back or suppressed.", [
            "none", "slight", "moderate", "high", "very high",
        ]),
        "emotion_target": DecisionQuestion("choice", "Identify the main target of the expressed emotion.", {
            key: None for key in ("self", "other_person", "situation", "mixed", "unclear")
        }),
        "emotion_shift": DecisionQuestion("choice", "Estimate how the expressed emotion changed from recent context.", {
            key: None for key in ("stable", "slight_shift", "strong_shift", "unclear")
        }),
        "needs_emotional_response": DecisionQuestion(
            "noul", "How strongly does this message call for an emotionally attuned response?"
        ),
        "memory_worthy": DecisionQuestion(
            "noul", "Could this message contain information useful in future conversations?"
        ),
        "importance": DecisionQuestion("score", "Rate long-term conversational importance.", [
            "Transient small talk with no future value",
            "Useful only for short-term context",
            "Information that may be useful later",
            "Stable preference, important plan, or relationship information",
            "Major personal event or significant shared experience",
        ]),
        "relationship_event": DecisionQuestion("choice", "Classify the relationship signal.", {
            key: None for key in (
                "relationship_positive", "relationship_negative", "neutral",
                "trust_signal", "closeness_signal", "conflict",
            )
        }),
        "requires_deep_reasoning": DecisionQuestion(
            "noul", "Does correct interpretation require multi-step reasoning or substantial hidden context?"
        ),
    }
    for signal in (
        "affection", "complaint", "teasing", "conflict",
        "reassurance_seeking", "vulnerability", "praise", "concern",
    ):
        questions[f"social_{signal}"] = DecisionQuestion(
            "noul", f"Does the message express {signal.replace('_', ' ')}?"
        )
    return questions


def memory_questions(count: int) -> dict[str, DecisionQuestion]:
    return {
        f"memory_{index}_relevant": DecisionQuestion(
            "noul",
            {
                "question": "Is this candidate memory genuinely relevant for understanding the current event?",
                "candidate_index": index,
            },
        )
        for index in range(max(0, count))
    }


def planner_questions(memory_ids: list[str]) -> dict[str, DecisionQuestion]:
    questions = {
        "action": DecisionQuestion("choice", "Choose Alice's bounded conversational action.", {
            key: None for key in (
                "reply", "acknowledge", "comfort", "clarify", "tease",
                "recall_then_reply", "wait", "ignore",
            )
        }),
        "use_memory": DecisionQuestion("noul", "Would using one supplied memory make the response more natural and useful?"),
        "ask_followup": DecisionQuestion("noul", "Should Alice ask a follow-up question in this response?"),
        "tone": DecisionQuestion("choice", "Choose the response tone.", {
            key: None for key in ("neutral", "gentle", "playful", "serious", "reserved", "warm")
        }),
        "response_length": DecisionQuestion("choice", "Choose an appropriate response length.", {
            key: None for key in ("short", "medium", "long")
        }),
        "directness": DecisionQuestion("score", "How direct should the response be?", [
            "Indirect and reserved", "Balanced", "Clear and direct",
        ]),
        "requires_deep_reasoning": DecisionQuestion(
            "noul", "Does choosing a safe and useful response action require multi-step reasoning?"
        ),
    }
    if memory_ids:
        criteria = {"none": "Do not use a memory"}
        criteria.update({memory_id: "Use this supplied candidate memory" for memory_id in memory_ids})
        questions["memory_choice"] = DecisionQuestion(
            "choice", "Choose at most one supplied memory, or none.", criteria
        )
    return questions


def proactive_questions() -> dict[str, DecisionQuestion]:
    return {
        "action": DecisionQuestion("choice", "Should Alice proactively contact the user now?", {
            "send": "A contextual, useful message is appropriate now",
            "defer": "The reason may be valid later but not now",
            "do_nothing": "Silence is preferable",
        }),
        "natural_reason_exists": DecisionQuestion("noul", "Is there a concrete, natural reason to initiate contact?"),
        "likely_to_interrupt": DecisionQuestion("noul", "Is a message likely to interrupt or burden the user?"),
        "relevant_context_exists": DecisionQuestion("noul", "Is the available context directly relevant to a useful proactive message?"),
        "appropriateness": DecisionQuestion("score", "Rate proactive contact appropriateness.", [
            "Clearly inappropriate", "Weak reason", "Plausible but uncertain",
            "Appropriate", "Strongly appropriate and timely",
        ]),
        "requires_deep_reasoning": DecisionQuestion(
            "noul", "Would this proactive decision require complex multi-step reasoning?"
        ),
    }


def media_questions(
    audio_ids: list[str],
    emoji_ids: list[str],
    *,
    voice_available: bool,
    emoji_available: bool,
) -> dict[str, DecisionQuestion]:
    questions: dict[str, DecisionQuestion] = {
        "requires_deep_reasoning": DecisionQuestion(
            "noul",
            "Would choosing optional voice or emoji decoration require complex reasoning or hidden context?",
        )
    }
    if voice_available:
        questions["send_voice"] = DecisionQuestion(
            "noul",
            "Would a voice rendition feel natural and useful for this response, rather than intrusive or awkward?",
        )
        if audio_ids:
            criteria = {"none": "Use the configured default reference voice"}
            criteria.update({item_id: "Use this supplied reference-audio candidate" for item_id in audio_ids})
            questions["voice_choice"] = DecisionQuestion(
                "choice", "Choose the best supplied voice reference, or the configured default.", criteria
            )
    if emoji_available:
        questions["send_emoji"] = DecisionQuestion(
            "noul",
            "Would one pre-screened emoji image add a natural expression that fits the assistant's reply itself? "
            "Ignore user-only context, quoted words, mood scores, and captions that do not match the reply; abstain if unsure.",
        )
        criteria = {"none": "Do not attach an emoji"}
        criteria.update({item_id: "Use this supplied, response-aligned emoji candidate" for item_id in emoji_ids})
        questions["emoji_choice"] = DecisionQuestion(
            "choice", "Choose at most one supplied candidate that clearly fits the reply, or none.", criteria
        )
    return questions
