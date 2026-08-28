"""The asynchronously iterable AgentRun facade."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Final, cast

from coding_agent.control import RunControl
from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    PendingMessage,
    PendingMessageKind,
)
from coding_agent.extensions import ExtensionError
from coding_agent.session import Session

_STREAM_END: Final = object()
RunSourceEvent = AgentEvent | AgentSessionEvent
RunSourceFactory = Callable[[RunControl], AsyncIterator[RunSourceEvent]]
RunEventObserver = Callable[[RunSourceEvent], None]
RunSettledObserver = Callable[[AgentRunResult], tuple[AgentSessionEvent, ...]]


class AgentRun(AsyncIterator[AgentSessionEvent]):
    """Own one run's event stream, lifecycle state, and final result."""

    def __init__(
        self,
        run_id: str,
        source_factory: RunSourceFactory,
        *,
        session: Session | None = None,
        initial_events: tuple[AgentSessionEvent, ...] = (),
        event_observer: RunEventObserver | None = None,
        settled_observer: RunSettledObserver | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._run_id = run_id
        self._state = AgentRunState.ACTIVE
        self._events: asyncio.Queue[AgentSessionEvent | object] = asyncio.Queue()
        self._result: asyncio.Future[AgentRunResult] = loop.create_future()
        self._iterator_claimed = False
        self._stream_exhausted = False
        self._session = session
        self._control = RunControl(run_id)
        self._terminal_lock = asyncio.Lock()
        self._cancel_requested = False
        self._event_observer = event_observer
        self._settled_observer = settled_observer
        self._settled_observed = False
        self._worker = loop.create_task(
            self._drive(source_factory(self._control), initial_events), name=f"agent-run:{run_id}"
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

        async with self._terminal_lock:
            if self._state is AgentRunState.ACTIVE and not self._cancel_requested:
                self._cancel_requested = True
                self._control.cancel_event.set()
                for message in self._control.drop_all():
                    self._events.put_nowait(
                        AgentSessionEvent.from_pending_message(
                            AgentSessionEventKind.MESSAGE_DROPPED,
                            self._run_id,
                            message,
                            0,
                        )
                    )
                self._worker.cancel()
        return await self.result()

    async def steer(self, text: str) -> PendingMessage:
        """Queue a Steering Message for the next authoritative drain point."""

        return await self._enqueue(PendingMessageKind.STEERING, text)

    async def follow_up(self, text: str) -> PendingMessage:
        """Queue a Follow-up Message for the next natural settled boundary."""

        return await self._enqueue(PendingMessageKind.FOLLOW_UP, text)

    async def _enqueue(self, kind: PendingMessageKind, text: str) -> PendingMessage:
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{kind.value} message must not be blank")
        async with self._terminal_lock:
            if self._state is not AgentRunState.ACTIVE or self._cancel_requested:
                raise RuntimeError(f"cannot queue {kind.value} message when AgentRun is not active")
            message = self._control.enqueue(kind, text)
            self._events.put_nowait(
                AgentSessionEvent.from_pending_message(
                    AgentSessionEventKind.MESSAGE_QUEUED,
                    self._run_id,
                    message,
                    self._control.queue_size(kind),
                )
            )
            return message

    async def _drive(
        self,
        source: AsyncIterator[RunSourceEvent],
        initial_events: tuple[AgentSessionEvent, ...],
    ) -> None:
        authoritative_message = None
        failure = None
        terminal_state = AgentRunState.FAILED
        terminal_message: AssistantMessage | None = None
        terminal_error: AgentError | None = None
        try:
            for event in initial_events:
                await self._events.put(event)
            async for source_event in source:
                if isinstance(source_event, AgentSessionEvent):
                    await self._publish_session_events((source_event,))
                    continue
                agent_event = source_event
                if self._session is not None:
                    await self._publish_session_events(self._session.drain_events())
                self._observe(agent_event)
                await self._events.put(AgentSessionEvent.from_agent_event(agent_event))
                if agent_event.kind is AgentEventKind.MESSAGE_END:
                    authoritative_message = agent_event.message
                    if self._session is not None and authoritative_message is not None:
                        self._session.record_authoritative_message(
                            authoritative_message, run_id=self._run_id
                        )
                        await self._publish_session_events(self._session.drain_events())
                elif agent_event.kind is AgentEventKind.ERROR:
                    failure = agent_event.error

            if failure is not None:
                terminal_error = failure
            elif authoritative_message is not None:
                terminal_state = AgentRunState.SETTLED
                terminal_message = authoritative_message
            else:
                terminal_error = AgentError(
                    code="kernel_missing_result",
                    message="AgentLoop ended without an authoritative message or error.",
                    source="kernel",
                )
        except asyncio.CancelledError:
            terminal_state = AgentRunState.CANCELLED
        except Exception as exc:
            is_extension_error = isinstance(exc, ExtensionError)
            terminal_error = AgentError(
                code=("extension_dispatch_error" if is_extension_error else "kernel_exception"),
                message=f"{type(exc).__name__}: {exc}",
                source="extension" if is_extension_error else "kernel",
            )
            await self._events.put(
                AgentSessionEvent.from_agent_event(
                    AgentEvent(
                        kind=AgentEventKind.ERROR,
                        run_id=self._run_id,
                        error=terminal_error,
                    )
                )
            )
        finally:
            close_source = getattr(source, "aclose", None)
            if callable(close_source):
                try:
                    await close_source()
                except asyncio.CancelledError:
                    terminal_state = AgentRunState.CANCELLED
                    terminal_message = None
                    terminal_error = None
                except Exception as exc:
                    is_extension_error = isinstance(exc, ExtensionError)
                    terminal_state = AgentRunState.FAILED
                    terminal_message = None
                    terminal_error = AgentError(
                        code=(
                            "extension_dispatch_error" if is_extension_error else "kernel_exception"
                        ),
                        message=f"source cleanup failed: {type(exc).__name__}: {exc}",
                        source="extension" if is_extension_error else "kernel",
                    )
                    await self._events.put(
                        AgentSessionEvent.from_agent_event(
                            AgentEvent(
                                kind=AgentEventKind.ERROR,
                                run_id=self._run_id,
                                error=terminal_error,
                            )
                        )
                    )
            try:
                await self._finish(
                    terminal_state,
                    message=terminal_message,
                    error=terminal_error,
                )
            except Exception as exc:
                is_extension_error = isinstance(exc, ExtensionError)
                terminal_error = AgentError(
                    code=("extension_dispatch_error" if is_extension_error else "kernel_exception"),
                    message=f"{type(exc).__name__}: {exc}",
                    source="extension" if is_extension_error else "kernel",
                )
                await self._events.put(
                    AgentSessionEvent.from_agent_event(
                        AgentEvent(
                            kind=AgentEventKind.ERROR,
                            run_id=self._run_id,
                            error=terminal_error,
                        )
                    )
                )
                await self._finish(AgentRunState.FAILED, error=terminal_error)

    async def _finish(
        self,
        state: AgentRunState,
        *,
        message: AssistantMessage | None = None,
        error: AgentError | None = None,
    ) -> None:
        async with self._terminal_lock:
            if self._state is not AgentRunState.ACTIVE:
                return

            result = AgentRunResult(
                run_id=self._run_id,
                state=state,
                message=message,
                error=error,
            )
            if self._settled_observer is not None and not self._settled_observed:
                self._settled_observed = True
                try:
                    await self._publish_session_events(self._settled_observer(result))
                except ExtensionError:
                    # agent_settled runs after the canonical result is fixed; its
                    # separate ExtensionEvent failure cannot rewrite that terminal.
                    pass
            self._state = state
            self._events.put_nowait(AgentSessionEvent.from_result(result))
            self._result.set_result(result)
            self._events.put_nowait(_STREAM_END)

    def _observe(self, event: RunSourceEvent) -> None:
        if self._event_observer is not None:
            self._event_observer(event)

    async def _publish_session_events(
        self,
        events: tuple[AgentSessionEvent, ...],
    ) -> None:
        """Publish and observe all post-commit events without rewriting Session state."""

        for event in events:
            await self._events.put(event)
        for event in events:
            try:
                self._observe(event)
            except ExtensionError:
                # Session observation is post-commit. ExtensionEvent records the
                # failure; the canonical operation and remaining Hooks continue.
                continue
