from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """An authenticated caller, never an owner supplied in command arguments."""

    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not self.user_id or not self.session_id:
            raise ValueError("memory management requires a user and session")
