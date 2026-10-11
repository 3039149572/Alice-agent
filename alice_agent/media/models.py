from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    id: str
    kind: str
    path: str
    description: str = ""
    reference_text: str = ""
    vad: tuple[float, float, float] | None = None
    energy: float | None = None
    caption: str = ""
    intent: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RankedMediaCandidate:
    candidate: MediaCandidate
    score: float


@dataclass(frozen=True, slots=True)
class MediaDecision:
    send_voice: bool = False
    send_emoji: bool = False
    voice: MediaCandidate | None = None
    emoji: MediaCandidate | None = None
    source: str = "deterministic"
    reason: str = "media disabled"
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MediaConfig:
    enabled: bool = False
    voice_enabled: bool = True
    emoji_enabled: bool = True
    jev_enabled: bool = True
    min_chars: int = 10
    cooldown_seconds: float = 0.0
    voice_probability: float = 1.0
    emoji_probability: float = 1.0
    candidate_limit: int = 5
    audio_score_threshold: float = 0.30
    emoji_score_threshold: float = 0.30
    audio_index_path: str = ""
    emoji_index_path: str = ""
    audio_dir: str = ""
    emoji_dir: str = ""
    genie_host: str = "localhost"
    genie_port: int = 8000
    character_name: str = ""
    onnx_model_dir: str = ""
    language: str = "japanese"
    deepl_api_key: str = field(default="", repr=False)
    default_ref_audio: str = ""
    default_ref_text: str = ""
    send_text_with_audio: bool = True
    tts_max_chars: int = 80
    emoji_delay_seconds: float = 0.5
    log_tts_inputs: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_chars", max(1, int(self.min_chars)))
        object.__setattr__(self, "cooldown_seconds", max(0.0, float(self.cooldown_seconds)))
        for name in (
            "voice_probability", "emoji_probability",
            "audio_score_threshold", "emoji_score_threshold",
        ):
            object.__setattr__(
                self, name, min(1.0, max(0.0, float(getattr(self, name))))
            )
        object.__setattr__(self, "candidate_limit", min(12, max(1, int(self.candidate_limit))))
        object.__setattr__(self, "genie_port", min(65535, max(1, int(self.genie_port))))
        language = str(self.language).strip().lower()
        if language not in {"chinese", "english", "japanese"}:
            language = "japanese"
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "deepl_api_key", str(self.deepl_api_key).strip())
        object.__setattr__(self, "tts_max_chars", min(500, max(10, int(self.tts_max_chars))))
        object.__setattr__(self, "emoji_delay_seconds", max(0.0, float(self.emoji_delay_seconds)))
