from __future__ import annotations

from astrbot.api.event.filter import CustomFilter


class AlicePrivateMessageFilter(CustomFilter):
    """Accept non-command text while leaving AstrBot wake-prefix commands alone."""

    def filter(self, event, cfg) -> bool:
        raw_text = getattr(getattr(event, "message_obj", None), "message_str", None)
        if raw_text is None:
            raw_text = getattr(event, "message_str", "")
        raw_text = str(raw_text or "").strip()
        if not raw_text:
            return False
        wake_prefixes = cfg.get("wake_prefix", []) if hasattr(cfg, "get") else []
        if isinstance(wake_prefixes, str):
            wake_prefixes = [wake_prefixes]
        return not any(
            prefix and raw_text.startswith(str(prefix)) for prefix in wake_prefixes
        )
