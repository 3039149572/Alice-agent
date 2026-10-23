from __future__ import annotations

from dataclasses import asdict, replace

from ..domain.event import AgentEvent
from ..domain.perception import PerceptionResult
from ..domain.state import AgentState
from ..ports.storage import StoragePort
from .reducer import StateReducer
from .time_reducer import TimeStateReducer
from ..daily_events.mood import DailyMoodReducer


class StateManager:
    _NAMESPACE = "alice_state"

    def __init__(
        self,
        storage: StoragePort,
        reducer: StateReducer,
        time_reducer: TimeStateReducer | None = None,
    ) -> None:
        self._storage = storage
        self._reducer = reducer
        self._time_reducer = time_reducer or TimeStateReducer()
        self._daily_reducer = DailyMoodReducer()

    async def get(self, session_id: str) -> AgentState:
        data = await self._storage.get(self._NAMESPACE, session_id)
        if isinstance(data, AgentState):
            return data
        if isinstance(data, dict):
            return AgentState(**data)
        return AgentState()

    async def update(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
    ) -> AgentState:
        new_state = await self.preview_update(event, perception)
        await self.commit_update(event.session_id, new_state)
        return new_state

    async def preview_update(
        self,
        event: AgentEvent,
        perception: PerceptionResult,
    ) -> AgentState:
        """Compute a proposed turn state without persisting it before delivery."""
        old_state = await self.get(event.session_id)
        return self._reducer.reduce(old_state, perception, event)

    async def update_once(self, event: AgentEvent, perception: PerceptionResult, operation_id: str) -> AgentState:
        def transform(value):
            state = AgentState(**value) if isinstance(value, dict) else AgentState()
            return asdict(self._reducer.reduce(state, perception, event))
        result = await self._storage.mutate_once(self._NAMESPACE, event.session_id, operation_id, transform)
        return AgentState(**result)

    async def clear_context(self, session_id: str) -> None:
        state = await self.get(session_id)
        await self.commit_update(session_id, replace(state, current_topic=None, daily_events=[], topic_hooks=[]))

    async def commit_update(self, session_id: str, state: AgentState) -> None:
        await self._storage.set(self._NAMESPACE, session_id, asdict(state))

    async def advance_time(
        self, session_id: str, elapsed_seconds: float, *,
        inactivity_seconds: float | None = None, rest_need: float = 0.0,
    ) -> AgentState:
        old_state = await self.get(session_id)
        new_state = self._time_reducer.reduce(
            old_state, elapsed_seconds,
            inactivity_seconds=inactivity_seconds, rest_need=rest_need,
        )
        await self._storage.set(self._NAMESPACE, session_id, asdict(new_state))
        return new_state

    async def apply_daily(self, session_id: str, day: str, events: list[dict]) -> AgentState:
        old_state = await self.get(session_id)
        if old_state.daily_event_day is not None and old_state.daily_event_day >= day:
            return old_state
        new_state = self._daily_reducer.reduce(old_state, day, events)
        await self._storage.set(self._NAMESPACE, session_id, asdict(new_state))
        return new_state

    async def close(self) -> None:
        await self._storage.close()
