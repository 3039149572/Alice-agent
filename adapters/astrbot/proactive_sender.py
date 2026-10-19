from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ...alice_agent.ports.proactive import BeforeSend, OnDelivered, ProactiveSendResult
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .text_segments import split_text


class AstrBotProactiveSender:
    """The only place where autonomous core output becomes an AstrBot message."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        split_enabled: bool = False,
        split_max_chars: int = 500,
        split_interval_seconds: float = 1.0,
        session_lock: Callable[[str], asyncio.Lock] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._context = context
        self._logger = logger
        self._split_enabled = split_enabled
        self._split_max_chars = split_max_chars
        self._split_interval_seconds = split_interval_seconds
        self._session_lock = session_lock
        self._sleep = sleep_func or asyncio.sleep

    async def send(
        self,
        session_id: str,
        text: str,
        *,
        before_send: BeforeSend | None = None,
        on_delivered: OnDelivered | None = None,
    ) -> ProactiveSendResult:
        if not text.strip():
            return ProactiveSendResult(False)

        try:
            chunks = (
                split_text(text, self._split_max_chars)
                if self._split_enabled
                else [text]
            )
            if self._session_lock is None:
                outcome = await self._send_chunks(session_id, chunks, before_send)
                await self._notify_delivery(session_id, outcome, on_delivered)
                return outcome
            async with self._session_lock(session_id):
                outcome = await self._send_chunks(session_id, chunks, before_send)
                await self._notify_delivery(session_id, outcome, on_delivered)
                return outcome
        except Exception:
            self._logger.exception(
                f"Alice proactive send failed session={session_id}"
            )
            return ProactiveSendResult(False)

    async def _send_chunks(
        self,
        session_id: str,
        chunks: list[str],
        before_send: BeforeSend | None,
    ) -> ProactiveSendResult:
        delivered = 0
        delivered_text: list[str] = []
        for index, chunk in enumerate(chunks):
            if before_send is not None:
                try:
                    still_current = await before_send()
                except Exception:
                    self._logger.exception(
                        f"Alice proactive revision check failed session={session_id}"
                    )
                    return ProactiveSendResult(False, delivered, "".join(delivered_text))
                if not still_current:
                    return ProactiveSendResult(False, delivered, "".join(delivered_text))
            try:
                result = await self._context.send_message(
                    session_id, MessageChain([Plain(chunk)])
                )
            except Exception:
                self._logger.exception(
                    f"Alice proactive send failed session={session_id}"
                )
                return ProactiveSendResult(False, delivered, "".join(delivered_text))
            if result is False:
                self._logger.warning(
                    f"PROACTIVE sender_rejected session={session_id}; "
                    "AstrBot returned False: check platform/session routing and adapter logs"
                )
                return ProactiveSendResult(False, delivered, "".join(delivered_text))
            delivered += 1
            delivered_text.append(chunk)
            if (
                index + 1 < len(chunks)
                and self._split_interval_seconds > 0
            ):
                await self._sleep(self._split_interval_seconds)
        return ProactiveSendResult(True, delivered, "".join(delivered_text))

    async def _notify_delivery(
        self,
        session_id: str,
        outcome: ProactiveSendResult,
        callback: OnDelivered | None,
    ) -> None:
        if callback is None or not outcome.delivered_text:
            return
        try:
            await callback(outcome.delivered_text)
        except Exception:
            self._logger.exception(
                f"Alice proactive history commit failed session={session_id}"
            )
