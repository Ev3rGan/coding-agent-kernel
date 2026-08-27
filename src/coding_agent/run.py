"""The asynchronously iterable AgentRun facade."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Final, cast

from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AssistantMessage,
)
from coding_agent.session import Session

_STREAM_END: Final = object()


class AgentRun(AsyncIterator[AgentSessionEvent]):
    """Own one run's event stream, lifecycle state, and final result."""

    def __init__(
        self,
        run_id: str,
        source: AsyncIterator[AgentEvent],
        *,
        session: Session | None = None,
        initial_events: tuple[AgentSessionEvent, ...] = (),
    ) -> None:
        loop = asyncio.get_running_loop()
        self._run_id = run_id
        self._state = AgentRunState.ACTIVE
        self._events: asyncio.Queue[AgentSessionEvent | object] = asyncio.Queue()
        self._result: asyncio.Future[AgentRunResult] = loop.create_future()
        self._iterator_claimed = False
        self._stream_exhausted = False
        self._session = session
        self._worker = loop.create_task(
            self._drive(source, initial_events), name=f"agent-run:{run_id}"
        )

    @property
    def run_id(self) -> str:
        """The deterministic identifier for this run."""

        return self._run_id

    @property
    def state(self) -> AgentRunState:
        """The current run state."""

        return self._state

    def __aiter__(self) -> AgentRun:
        if self._iterator_claimed:
            raise RuntimeError("an AgentRun Event Stream has exactly one consumer")
        self._iterator_claimed = True
        return self

    async def __anext__(self) -> AgentSessionEvent:
        if self._stream_exhausted:
            raise StopAsyncIteration
        item = await self._events.get()
        if item is _STREAM_END:
            self._stream_exhausted = True
            raise StopAsyncIteration
        return cast(AgentSessionEvent, item)

    async def result(self) -> AgentRunResult:
        """Wait for and return the run's final result without consuming events."""

        return await asyncio.shield(self._result)

    async def wait(self) -> AgentRunResult:
        """Alias for result(), emphasizing settlement rather than retrieval."""

        return await self.result()

    async def cancel(self) -> AgentRunResult:
        """Cancel active Provider or Tool Execution work and await settlement."""

        if self._state is AgentRunState.ACTIVE:
            self._worker.cancel()
        return await self.result()

    async def _drive(
        self,
        source: AsyncIterator[AgentEvent],
        initial_events: tuple[AgentSessionEvent, ...],
    ) -> None:
        authoritative_message = None
        failure = None
        try:
            for event in initial_events:
                await self._events.put(event)
            async for agent_event in source:
                if self._session is not None:
                    for session_event in self._session.drain_events():
                        await self._events.put(session_event)
                await self._events.put(AgentSessionEvent.from_agent_event(agent_event))
                if agent_event.kind is AgentEventKind.MESSAGE_END:
                    authoritative_message = agent_event.message
                    if self._session is not None and authoritative_message is not None:
                        self._session.record_authoritative_message(
                            authoritative_message, run_id=self._run_id
                        )
                        for session_event in self._session.drain_events():
                            await self._events.put(session_event)
                elif agent_event.kind is AgentEventKind.ERROR:
                    failure = agent_event.error

            if failure is not None:
                await self._finish(AgentRunState.FAILED, error=failure)
            elif authoritative_message is not None:
                await self._finish(AgentRunState.SETTLED, message=authoritative_message)
            else:
                await self._finish(
                    AgentRunState.FAILED,
                    error=AgentError(
                        code="kernel_missing_result",
                        message="AgentLoop ended without an authoritative message or error.",
                        source="kernel",
                    ),
                )
        except asyncio.CancelledError:
            await self._finish(AgentRunState.CANCELLED)
        except Exception as exc:
            error = AgentError(
                code="kernel_exception",
                message=f"{type(exc).__name__}: {exc}",
                source="kernel",
            )
            await self._events.put(
                AgentSessionEvent.from_agent_event(
                    AgentEvent(
                        kind=AgentEventKind.ERROR,
                        run_id=self._run_id,
                        error=error,
                    )
                )
            )
            await self._finish(AgentRunState.FAILED, error=error)

    async def _finish(
        self,
        state: AgentRunState,
        *,
        message: AssistantMessage | None = None,
        error: AgentError | None = None,
    ) -> None:
        if self._state is not AgentRunState.ACTIVE:
            return

        result = AgentRunResult(
            run_id=self._run_id,
            state=state,
            message=message,
            error=error,
        )
        self._state = state
        await self._events.put(AgentSessionEvent.from_result(result))
        self._result.set_result(result)
        await self._events.put(_STREAM_END)
