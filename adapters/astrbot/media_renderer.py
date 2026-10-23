from __future__ import annotations

import asyncio
import json
import re
import uuid
import wave
import time
import weakref
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
    spoken_source_text: str | None = None
    timed_out: bool = False


_BACKEND_LOCKS = weakref.WeakKeyDictionary()


def _backend_lock(base_url: str, character: str) -> asyncio.Lock:
    """Genie reference state is shared by character, including renderer instances."""
    loop = asyncio.get_running_loop()
    locks = _BACKEND_LOCKS.setdefault(loop, weakref.WeakValueDictionary())
    key = (base_url, character)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


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
        self._audio_leases: dict[str, float | None] = {}
        self._character_loaded = False

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._config.render_timeout_seconds))
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
        try:
            await asyncio.wait_for(self._initialize(), self._config.render_timeout_seconds)
        except TimeoutError:
            self._logger.warning("Alice media: initialization budget exhausted")

    async def _initialize(self) -> None:
        self._cleanup_stale()
        if not self.voice_runtime_available:
            return
        try:
            async with _backend_lock(self._base_url, self._config.character_name):
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
        cutoff = time.time() - 86400.0
        for item in self._temp_dir.glob("alice_tts_*.wav"):
            try:
                if not item.is_symlink() and item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                pass

    def release(self, audio_path: str) -> None:
        """Release only this renderer's output, with an upload grace period.

        A yielded Record is not a platform receipt. Keep released files for at
        least five minutes; never evict another active render's file.
        """
        if audio_path in self._audio_leases:
            self._audio_leases[audio_path] = time.time()
        self._cleanup_released()

    def _cleanup_released(self) -> None:
        cutoff = time.time() - 300.0
        for name, released_at in list(self._audio_leases.items()):
            if released_at is None or released_at > cutoff:
                continue
            try:
                path = Path(name)
                if path.parent == self._temp_dir and not path.is_symlink():
                    path.unlink(missing_ok=True)
                self._audio_leases.pop(name, None)
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
        path = str(target)
        self._audio_leases[path] = None
        return path

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
        self, session_id: str, response_text: str, decision: MediaDecision | None,
    ) -> RenderedMedia:
        try:
            return await asyncio.wait_for(
                self._render(session_id, response_text, decision),
                timeout=self._config.render_timeout_seconds,
            )
        except TimeoutError:
            self._logger.warning("Alice media: render budget exhausted; continuing with text")
            emoji = None
            if decision and decision.send_emoji and decision.emoji and Path(decision.emoji.path).is_file():
                emoji = decision.emoji.path
            return RenderedMedia(emoji_path=emoji, timed_out=True)

    async def _render(
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
        prepared = None
        if decision.send_voice and self.voice_runtime_available:
            try:
                reference_path = self._config.default_ref_audio
                reference_text = self._config.default_ref_text
                if decision.voice is not None:
                    reference_path = decision.voice.path
                    reference_text = decision.voice.reference_text
                prepared = self._prepare_text(response_text, self._config.tts_max_chars)
                if len(prepared) < 2:
                    raise ValueError("TTS response text is too short")
                spoken = await self._translate(session_id, prepared)
                # Translation is independent and can overlap other sessions.
                # The server's reference mutation and synthesis must be atomic.
                async with _backend_lock(self._base_url, self._config.character_name):
                    if not self._character_loaded:
                        await self._load_character()
                    if not (reference_path and reference_text and Path(reference_path).is_file()):
                        reference_path = self._config.default_ref_audio or self._current_ref_path
                        reference_text = self._config.default_ref_text or self._current_ref_text
                    if not (reference_path and reference_text):
                        raise FileNotFoundError("no usable TTS reference audio")
                    # Always set the chosen reference: another renderer sharing
                    # this backend may have changed it since our previous turn.
                    await self._set_reference(reference_path, reference_text)
                    if self._config.log_tts_inputs:
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
                self._cleanup_released()
            except Exception as exc:
                self._logger.warning(
                    f"Alice media: TTS failed safely ({type(exc).__name__})"
                )
        return RenderedMedia(audio_path=audio_path, emoji_path=emoji_path,
                             spoken_source_text=prepared if audio_path else None)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
