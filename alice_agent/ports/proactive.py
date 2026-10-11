from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProactiveSendResult:
    """What the sender accepted before finishing or refusing a message."""

    completed: bool
    delivered_chunks: int = 0
    delivered_text: str = ""

    @property
    def partial(self) -> bool:
        return not self.completed and self.delivered_chunks > 0

    def __bool__(self) -> bool:
        return self.completed


BeforeSend = Callable[[], Awaitable[bool]]
OnDelivered = Callable[[str], Awaitable[None]]


class ProactiveSenderPort(Protocol):
    async def send(
        self,
        session_id: str,
        text: str,
        *,
        before_send: BeforeSend | None = None,
        on_delivered: OnDelivered | None = None,
    ) -> ProactiveSendResult: ...
