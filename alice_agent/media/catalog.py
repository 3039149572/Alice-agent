from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import MediaCandidate


def _clamp01(value: Any) -> float | None:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def _vad(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    converted = tuple(_clamp01(item) for item in value)
    if any(item is None for item in converted):
        return None
    return converted  # type: ignore[return-value]


def _tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        return ()
    return tuple(str(item).strip()[:48] for item in values if str(item).strip())[:12]


def _caption(raw: dict[str, Any], description: str) -> str:
    explicit = str(raw.get("caption") or "").strip()
    if explicit:
        return explicit[:120]
    # Old indexes keep on-image text inside `desc`. Preserve it separately so
    # semantic matching can distinguish a caption from the depicted action.
    quoted = re.findall(
        r"“([^”]{1,80})”|‘([^’]{1,80})’|「([^」]{1,80})」|『([^』]{1,80})』|\"([^\"]{1,80})\"|'([^']{1,80})'",
        description,
    )
    values = [next((part for part in groups if part), "") for groups in quoted]
    if not values:
        bare = re.search(
            r"(?:配文|配有文字|文字(?:为|是)|写着|写有)\s*([A-Za-z][A-Za-z0-9~!！?？_-]{1,40})",
            description,
            re.IGNORECASE,
        )
        if bare:
            values.append(bare.group(1))
    return " / ".join(dict.fromkeys(value.strip() for value in values if value.strip()))[:120]


class MediaCatalog:
    def __init__(
        self,
        audio: list[MediaCandidate] | None = None,
        emoji: list[MediaCandidate] | None = None,
    ) -> None:
        self.audio = audio or []
        self.emoji = emoji or []

    @staticmethod
    def _load_list(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    @staticmethod
    def _resolve_path(raw_path: Any, configured_dir: Path | None, index_path: Path, kind: str) -> str:
        raw = str(raw_path or "").strip()
        if not raw:
            return ""
        normalized_name = Path(raw.replace("\\", "/")).name
        candidates: list[Path] = []
        if configured_dir is not None:
            candidates.append(configured_dir / normalized_name)
        direct = Path(raw)
        if direct.is_absolute():
            candidates.append(direct)
        candidates.extend(
            (
                index_path.parent / raw,
                index_path.parent / ("audio" if kind == "audio" else "images") / normalized_name,
            )
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        return str(candidates[0]) if candidates else raw

    @classmethod
    def _items(
        cls,
        path: Path,
        configured_dir: Path | None,
        kind: str,
    ) -> list[MediaCandidate]:
        items: list[MediaCandidate] = []
        for index, raw in enumerate(cls._load_list(path)):
            resolved = cls._resolve_path(raw.get("path"), configured_dir, path, kind)
            if not resolved or not Path(resolved).is_file():
                continue
            items.append(
                MediaCandidate(
                    id=f"{kind}_{index}",
                    kind=kind,
                    path=resolved,
                    description=str(raw.get("desc") or "")[:240],
                    reference_text=str(raw.get("text") or "")[:500],
                    vad=_vad(raw.get("vad")),
                    energy=_clamp01(raw.get("energy")),
                    caption=_caption(raw, str(raw.get("desc") or "")) if kind == "emoji" else "",
                    intent=str(raw.get("intent") or "").strip()[:48] if kind == "emoji" else "",
                    tags=_tags(raw.get("tags")) if kind == "emoji" else (),
                )
            )
        return items

    @classmethod
    def from_files(
        cls,
        *,
        audio_index_path: str = "",
        emoji_index_path: str = "",
        audio_dir: str = "",
        emoji_dir: str = "",
    ) -> "MediaCatalog":
        audio_path = Path(audio_index_path) if audio_index_path else Path("__missing_audio_index__")
        emoji_path = Path(emoji_index_path) if emoji_index_path else Path("__missing_emoji_index__")
        audio_root = Path(audio_dir) if audio_dir else None
        emoji_root = Path(emoji_dir) if emoji_dir else None
        return cls(
            cls._items(audio_path, audio_root, "audio"),
            cls._items(emoji_path, emoji_root, "emoji"),
        )
