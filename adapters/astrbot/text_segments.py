from __future__ import annotations

import re


_PARAGRAPH_BREAK = re.compile(r"(?:\r?\n[ \t]*){2,}")
_SENTENCE_END = re.compile(r"[.!?。！？…]+[\"'’”」』）)】》]*[ \t]*")
_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Return complete fenced-code ranges, including their fence lines."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    opening_start: int | None = None
    opening_char = ""
    opening_size = 0

    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        body = line.rstrip("\r\n")
        match = _FENCE_LINE.match(body)

        if opening_start is None:
            if match:
                fence = match.group(1)
                info = match.group(2)
                # CommonMark does not allow backticks in a backtick fence's info string.
                if fence[0] == "~" or "`" not in info:
                    opening_start = offset
                    opening_char = fence[0]
                    opening_size = len(fence)
        elif match:
            fence = match.group(1)
            remainder = match.group(2)
            if (
                fence[0] == opening_char
                and len(fence) >= opening_size
                and not remainder.strip()
            ):
                ranges.append((opening_start, line_end))
                opening_start = None
                opening_char = ""
                opening_size = 0

        offset = line_end

    if opening_start is not None:
        # An unfinished fence is still treated as one protected range. The splitter
        # cannot add a missing closing fence without changing the source text.
        ranges.append((opening_start, len(text)))
    return ranges


def _inside_fence(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < position < end for start, end in ranges)


def _candidate_ends(
    text: str,
    pattern: re.Pattern[str],
    fence_ranges: list[tuple[int, int]],
) -> list[int]:
    ends: list[int] = []
    for match in pattern.finditer(text):
        end = match.end()
        if end <= match.start() or _inside_fence(end, fence_ranges):
            continue
        ends.append(end)
    return ends


def split_text(text: str, max_chars: int) -> list[str]:
    """Send complete sentences (or paragraphs), never a character fragment.

    ``max_chars`` remains a soft compatibility setting: an individual sentence
    or fenced block may exceed it. Joining the parts reproduces ``text`` exactly.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    if not text:
        return []

    fences = _fenced_ranges(text)
    boundaries = set(_candidate_ends(text, _PARAGRAPH_BREAK, fences))
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if _inside_fence(end, fences):
            continue
        # A dot between digits or inside a hostname is not a sentence end.
        if match.group(0).startswith("."):
            before = text[match.start() - 1] if match.start() else ""
            after = text[end] if end < len(text) else ""
            if before.isalnum() and after.isalnum():
                continue
        boundaries.add(end)
    for fence_start, fence_end in fences:
        boundaries.add(fence_start)
        boundaries.add(fence_end)
    boundaries.add(len(text))

    chunks: list[str] = []
    start = 0
    for end in sorted(boundaries):
        if end > start:
            chunks.append(text[start:end])
            start = end

    # A pure-whitespace part is commonly dropped by chat platforms. Attach such
    # parts to the closest text part, allowing that part to exceed the soft limit.
    coalesced: list[str] = []
    leading_whitespace = ""
    for chunk in chunks:
        if chunk.strip():
            if leading_whitespace:
                chunk = leading_whitespace + chunk
                leading_whitespace = ""
            coalesced.append(chunk)
        elif coalesced:
            coalesced[-1] += chunk
        else:
            leading_whitespace += chunk

    if leading_whitespace:
        if coalesced:
            coalesced[-1] += leading_whitespace
        else:
            # The whole input is whitespace. Keep the splitter's size behavior;
            # the sender rejects this input before attempting delivery.
            coalesced.extend(chunks)

    return coalesced
