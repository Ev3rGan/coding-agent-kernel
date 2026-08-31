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
from coding_agent.permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionRequest,
    make_permission_request_decision,
)
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
        permission_mode: PermissionMode = PermissionMode.AUTO,
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
        self._permission_mode = permission_mode
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

    @property
    def permission_mode(self) -> PermissionMode:
        """The immutable Host-selected Permission Mode for this Agent Run."""

        return self._permission_mode

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

        return await self._cancel("Host cancelled the pending Permission Request")

    async def _cancel(self, pending_reason: str) -> AgentRunResult:
        async with self._terminal_lock:
            if self._state is AgentRunState.ACTIVE and not self._cancel_requested:
                self._cancel_requested = True
                self._control.cancel_event.set()
                pending_request = self._control.invalidate_permission()
                if pending_request is not None:
                    decision = make_permission_request_decision(
                        pending_request,
                        approved=False,
                        reason=pending_reason,
                    )
                    await self._publish_permission_resolution(pending_request, decision)
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

    async def aclose(self) -> None:
        """Treat an Event Stream Host disconnect as cancellation and cleanup."""

        await self._cancel("Host disconnected with a pending Permission Request")

    async def resolve_permission(self, request_id: str, approved: bool) -> None:
        """Resolve the one currently pending Permission Request exactly once."""

        async with self._terminal_lock:
            if self._state is not AgentRunState.ACTIVE or self._cancel_requested:
                raise RuntimeError("cannot resolve permission when AgentRun is not active")
            request = self._control.validate_permission_resolution(request_id, approved)
            decision = make_permission_request_decision(
                request,
                approved=approved,
                reason=(
                    "Host approved the one-time Permission Request"
                    if approved
                    else "Host denied the one-time Permission Request"
                ),
            )
            await self._publish_permission_resolution(request, decision)
            self._control.resolve_permission(request_id, decision)

    async def _publish_permission_resolution(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> None:
        if self._session is not None:
            self._session.record_permission_decision(decision, run_id=self._run_id)
        await self._publish_session_events(
            (
                AgentSessionEvent.from_permission_decision(
                    self._run_id,
                    request.request_id,
                    decision,
                ),
            )
        )
        if self._session is not None:
            await self._publish_session_events(self._session.drain_events())

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
                elif agent_event.kind is AgentEventKind.TOOL_EXECUTION_END:
                    tool_result = agent_event.tool_result
                    if self._session is not None and tool_result is not None:
                        self._session.record_tool_result(tool_result, run_id=self._run_id)
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

            self._control.invalidate_permission()

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
