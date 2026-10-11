from __future__ import annotations

import json
import os
import re
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...alice_agent.domain.llm import LLMRequest
from ...alice_agent.llm.service import LLMService
from ...alice_agent.media.models import MediaConfig, MediaDecision


@dataclass(frozen=True, slots=True)
class RenderedMedia:
    audio_path: str | None = None
    emoji_path: str | None = None


class AstrBotMediaRenderer:
    """AstrBot-side I/O for Genie TTS and local emoji files."""

    _LANGUAGE_NAMES = {
        "chinese": ("Chinese", "中文"),
        "english": ("English", "英文"),
        "japanese": ("Japanese", "日文"),
    }

    def __init__(
        self,
        config: MediaConfig,
        llm: LLMService,
        logger: object,
        temp_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._logger = logger
        self._temp_dir = temp_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._base_url = f"http://{config.genie_host}:{config.genie_port}"
        self._owns_client = client is None
        self._client = client
        self._current_ref_path: str | None = None
        self._current_ref_text: str | None = None
        self._last_audio_path: str | None = None
        self._character_loaded = False

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        return self._client

    @property
    def voice_runtime_available(self) -> bool:
        return bool(
            self._config.enabled
            and self._config.voice_enabled
            and self._config.character_name.strip()
            and self._config.onnx_model_dir.strip()
        )

    async def initialize(self) -> None:
        self._cleanup_stale()
        if not self.voice_runtime_available:
            return
        try:
            await self._load_character()
            if self._default_reference_available():
                await self._set_reference(
                    self._config.default_ref_audio,
                    self._config.default_ref_text,
                )
            self._logger.info("Alice media: Genie character initialized")
        except Exception as exc:
            self._logger.warning(
                f"Alice media: Genie initialization failed safely ({type(exc).__name__})"
            )

    async def _load_character(self) -> None:
        language = self._LANGUAGE_NAMES[self._config.language][0]
        response = await self._http().post(
            f"{self._base_url}/load_character",
            json={
                "character_name": self._config.character_name,
                "onnx_model_dir": self._config.onnx_model_dir,
                "language": language,
            },
        )
        response.raise_for_status()
        self._character_loaded = True

    def _cleanup_stale(self) -> None:
        for item in self._temp_dir.glob("alice_tts_*.wav"):
            try:
                item.unlink()
            except OSError:
                pass

    def _default_reference_available(self) -> bool:
        return bool(
            self._config.default_ref_audio
            and self._config.default_ref_text
            and Path(self._config.default_ref_audio).is_file()
        )

    @staticmethod
    def _prepare_text(text: str, limit: int) -> str:
        prepared = text.split("\n", 1)[0].strip()
        prepared = re.sub(r"https?://\S+", "", prepared)
        prepared = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", prepared)
        prepared = re.sub(r"[（(][^）)]*[）)]", "", prepared)
        prepared = re.sub(r"[*_~`#>\-]", "", prepared)
        prepared = re.sub(r"\s+", " ", prepared).strip()
        return prepared[:limit].rstrip()

    async def _translate(self, session_id: str, text: str) -> str:
        if self._config.language == "chinese":
            return text
        if self._config.deepl_api_key:
            try:
                return await self._translate_with_deepl(text)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
                self._logger.warning(
                    f"Alice media: DeepL translation failed, falling back to session model ({type(exc).__name__})"
                )
        target = self._LANGUAGE_NAMES[self._config.language][1]
        result = await self._llm.generate(
            LLMRequest(
                stage="media_translation",
                session_id=session_id,
                system_prompt=(
                    f"你是翻译器。把输入翻译成自然的{target}，只输出译文本身，"
                    "不得解释、加引号或添加前后缀。"
                ),
                prompt=text,
            )
        )
        translated = result.text.strip()
        if not translated:
            raise ValueError("media translation returned empty text")
        return translated

    async def _translate_with_deepl(self, text: str) -> str:
        api_host = "api-free.deepl.com" if self._config.deepl_api_key.endswith(":fx") else "api.deepl.com"
        target_lang = "JA" if self._config.language == "japanese" else "EN"
        response = await self._http().post(
            f"https://{api_host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {self._config.deepl_api_key}"},
            json={"text": [text], "target_lang": target_lang},
            timeout=8.0,
        )
        response.raise_for_status()
        data = response.json()
        translated = data["translations"][0]["text"]
        if not isinstance(translated, str) or not translated.strip():
            raise ValueError("DeepL translation returned empty text")
        return translated.strip()

    async def _set_reference(self, path: str, text: str) -> None:
        language = self._LANGUAGE_NAMES[self._config.language][0]
        response = await self._http().post(
            f"{self._base_url}/set_reference_audio",
            json={
                "character_name": self._config.character_name,
                "audio_path": path,
                "audio_text": text,
                "language": language,
            },
        )
        response.raise_for_status()
        self._current_ref_path = path
        self._current_ref_text = text

    def _save_audio(self, raw: bytes) -> str:
        target = self._temp_dir / f"alice_tts_{uuid.uuid4().hex}.wav"
        if len(raw) >= 4 and raw[:4] == b"RIFF":
            target.write_bytes(raw)
        else:
            with wave.open(str(target), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(32000)
                handle.writeframes(raw)
        return str(target)

    async def _synthesize(self, text: str) -> str:
        for attempt in range(2):
            response = await self._http().post(
                f"{self._base_url}/tts",
                json={
                    "character_name": self._config.character_name,
                    "text": text,
                    "split_sentence": False,
                },
            )
            if response.status_code == 404 and attempt == 0:
                path = self._current_ref_path or self._config.default_ref_audio
                reference_text = self._current_ref_text or self._config.default_ref_text
                if path and reference_text:
                    await self._load_character()
                    await self._set_reference(path, reference_text)
                    continue
            response.raise_for_status()
            return self._save_audio(response.content)
        raise RuntimeError("Genie TTS failed after resetting reference audio")

    async def render(
        self,
        session_id: str,
        response_text: str,
        decision: MediaDecision | None,
    ) -> RenderedMedia:
        if decision is None:
            return RenderedMedia()
        emoji_path = None
        if decision.send_emoji and decision.emoji and Path(decision.emoji.path).is_file():
            emoji_path = decision.emoji.path

        audio_path = None
        if decision.send_voice and self.voice_runtime_available:
            try:
                if not self._character_loaded:
                    await self._load_character()
                reference_path = self._config.default_ref_audio
                reference_text = self._config.default_ref_text
                if decision.voice is not None:
                    reference_path = decision.voice.path
                    reference_text = decision.voice.reference_text
                if reference_path and reference_text and Path(reference_path).is_file():
                    if reference_path != self._current_ref_path or reference_text != self._current_ref_text:
                        await self._set_reference(reference_path, reference_text)
                elif self._current_ref_path is None:
                    raise FileNotFoundError("no usable TTS reference audio")
                prepared = self._prepare_text(response_text, self._config.tts_max_chars)
                if len(prepared) < 2:
                    raise ValueError("TTS response text is too short")
                spoken = await self._translate(session_id, prepared)
                # Log the reference that Genie actually holds, rather than a
                # candidate path that may have fallen back to the current one.
                self._logger.info(
                    "Alice media: TTS inputs " + json.dumps(
                        {
                            "reference_audio": self._current_ref_path,
                            "reference_text": self._current_ref_text,
                            "source_text": prepared,
                            "translated_text": spoken,
                        },
                        ensure_ascii=False,
                    )
                )
                audio_path = await self._synthesize(spoken)
                previous = self._last_audio_path
                self._last_audio_path = audio_path
                if previous and previous != audio_path and os.path.isfile(previous):
                    try:
                        os.remove(previous)
                    except OSError:
                        pass
            except Exception as exc:
                self._logger.warning(
                    f"Alice media: TTS failed safely ({type(exc).__name__})"
                )
        return RenderedMedia(audio_path=audio_path, emoji_path=emoji_path)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
