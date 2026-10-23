from __future__ import annotations


def delivered_transcript(text_segments: object, spoken_source: str | None = None) -> str:
    """Combine actual text output and the source covered by a completed audio.

    Generated-but-unsent text is deliberately not an input. Unknown audio
    coverage cannot be inferred from the generated reply.
    """
    text = "".join(str(part) for part in text_segments) if isinstance(text_segments, list) else ""
    spoken = spoken_source or ""
    if not spoken or spoken in text:
        return text
    if not text or text in spoken:
        return spoken
    return text + "\n" + spoken
