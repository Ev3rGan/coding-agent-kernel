"""Headless AgentKernel orchestration."""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from functools import partial
from itertools import count
from typing import Literal, TypeVar, cast

from coding_agent.callout import dispose_awaitable
from coding_agent.context import (
    CompactionPlan,
    ContextConstructionError,
    ContextInput,
    ContextPipeline,
    ContextSettings,
    ModelContext,
    estimate_provider_request_characters,
)
from coding_agent.control import RetryPolicy, RunControl
from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    AssistantMessageAccumulator,
    PendingMessage,
    ProviderAbort,
    ProviderCancelled,
    ProviderDone,
    ProviderError,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ToolCall,
    ToolError,
    ToolProgress,
    ToolResult,
    validate_provider_stream_event,
)
from coding_agent.extensions import (
    BeforeAgentStartHookInput,
    Extension,
    ExtensionBlockedError,
    ExtensionDispatchError,
    ExtensionError,
    ExtensionEvent,
    ExtensionRegistrationError,
    ExtensionRuntime,
)
from coding_agent.permissions import (
    PermissionAction,
    PermissionDecision,
    PermissionMode,
    make_permission_decision,
    permission_classification_error_message,
)
from coding_agent.provider import (
    BranchSummaryMessage,
    ModelMessage,
    ModelProvider,
    ProviderRequest,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.run import AgentRun
from coding_agent.session import Session, SessionEntry, SessionStore
from coding_agent.tool_runtime import ToolRuntime


async def _put_progress(queue: asyncio.Queue[ToolProgress], progress: ToolProgress) -> None:
    await queue.put(progress)


class _ProviderOwnedCancellationError(RuntimeError):
    pass


T = TypeVar("T")


async def _isolated_provider_operation(
    operation: Callable[[], Awaitable[T]],
    *,
    cancellation_message: str,
) -> T:
    """Run one Provider-owned awaitable in a child task on the authoritative loop."""

    async def invoke() -> T:
        return await operation()

    worker = asyncio.create_task(invoke())
    try:
        return await worker
    except asyncio.CancelledError as exc:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        raise _ProviderOwnedCancellationError(cancellation_message) from exc
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def _isolated_provider_events(
    stream: AsyncIterator[object],
) -> AsyncIterator[object]:
    """Keep Provider-owned task cancellation distinct from Host Run cancellation."""

    while True:

        async def read_one() -> object:
            return await anext(stream)

        try:
            yield await _isolated_provider_operation(
                read_one,
                cancellation_message="Provider cancelled its own stream task",
            )
        except StopAsyncIteration:
            return


async def _isolated_provider_close(close: Callable[[], object]) -> None:
    async def close_one() -> None:
        result = close()
        if not inspect.isawaitable(result):
            raise TypeError("Provider stream aclose must be awaitable")
        await cast(Awaitable[object], result)

    await _isolated_provider_operation(
        close_one,
        cancellation_message="Provider cancelled its own cleanup task",
    )


async def _isolated_provider_factory(
    provider: ModelProvider,
    request: ProviderRequest,
) -> object:
    async def create_provider_stream() -> object:
        return provider.stream(request)

    return await _isolated_provider_operation(
        create_provider_stream,
        cancellation_message="Provider stream factory attempted cancellation",
    )


def _provider_failure_events(
    *,
    run_id: str,
    turn_id: str,
    message_id: str,
    message: AssistantMessage,
    provider_error: ProviderError,
    source: Literal["provider", "extension"] = "provider",
) -> tuple[AgentEvent, AgentEvent, AgentEvent]:
    """Close one failed provider response with a single lifecycle shape."""

    return (
        AgentEvent(
            kind=AgentEventKind.ERROR,
            run_id=run_id,
            turn_id=turn_id,
            message_id=message_id,
            message=message,
            provider_event=provider_error,
            error=AgentError(
                code=provider_error.code,
                message=provider_error.message,
                source=source,
            ),
        ),
        AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id),
        AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id),
    )


class AgentKernel:
    """Create AgentRun handles while keeping AgentLoop state behind one seam."""

    def __init__(
        self,
        provider: ModelProvider | str,
        *,
        tool_runtime: ToolRuntime | None = None,
        session: Session | None = None,
        context_pipeline: ContextPipeline | None = None,
        context_settings: ContextSettings | None = None,
        retry_policy: RetryPolicy | None = None,
        extensions: tuple[Extension, ...] = (),
        _extension_runtime: ExtensionRuntime | None = None,
    ) -> None:
        self._tool_runtime = tool_runtime
        self._session = session
        self._context_pipeline = context_pipeline or ContextPipeline()
        self._context_settings = context_settings or ContextSettings()
        self._retry_policy = retry_policy or RetryPolicy()
        self._extension_runtime = _extension_runtime or ExtensionRuntime(extensions)
        self._provider = self._resolve_provider(provider, self._extension_runtime)
        self._validate_extension_tools(self._extension_runtime, self._tool_runtime)
        if self._session is not None and self._extension_runtime.session_entry_types:
            try:
                self._session.register_entry_types(self._extension_runtime.session_entry_types)
            except (TypeError, ValueError) as exc:
                raise ExtensionRegistrationError(str(exc)) from exc
        if self._tool_runtime is not None and self._extension_runtime.tools:
            self._tool_runtime.register_many(self._extension_runtime.tools, enable=True)
        self._session_events: list[AgentSessionEvent] = []
        self._model_contexts: list[ModelContext] = []
        self._run_numbers = count(1)
        self._capture_session_events()

    @classmethod
    def with_new_session(
        cls,
        provider: ModelProvider | str,
        store: SessionStore,
        *,
        configuration: Mapping[str, object],
        session_id: str | None = None,
        entry_id_factory: Callable[[], str] | None = None,
        tool_runtime: ToolRuntime | None = None,
        context_pipeline: ContextPipeline | None = None,
        context_settings: ContextSettings | None = None,
        retry_policy: RetryPolicy | None = None,
        extensions: tuple[Extension, ...] = (),
    ) -> AgentKernel:
        """Create a Kernel that owns a new durable Session."""

        extension_runtime = ExtensionRuntime(extensions)
        resolved_provider = cls._resolve_provider(provider, extension_runtime)
        cls._validate_extension_tools(extension_runtime, tool_runtime)
        session = Session.create(
            store,
            configuration=configuration,
            session_id=session_id,
            entry_id_factory=entry_id_factory,
            entry_types=extension_runtime.session_entry_types,
        )
        return cls(
            resolved_provider,
            tool_runtime=tool_runtime,
            session=session,
            context_pipeline=context_pipeline,
            context_settings=context_settings,
            retry_policy=retry_policy,
            _extension_runtime=extension_runtime,
        )

    @classmethod
    def with_resumed_session(
        cls,
        provider: ModelProvider | str,
        store: SessionStore,
        session_id: str,
        *,
        entry_id_factory: Callable[[], str] | None = None,
        tool_runtime: ToolRuntime | None = None,
        context_pipeline: ContextPipeline | None = None,
        context_settings: ContextSettings | None = None,
        retry_policy: RetryPolicy | None = None,
        extensions: tuple[Extension, ...] = (),
    ) -> AgentKernel:
        """Create a Kernel after validating and resuming persisted Session history."""

        extension_runtime = ExtensionRuntime(extensions)
        resolved_provider = cls._resolve_provider(provider, extension_runtime)
        cls._validate_extension_tools(extension_runtime, tool_runtime)
        session = Session.resume(
            store,
            session_id,
            entry_id_factory=entry_id_factory,
            entry_types=extension_runtime.session_entry_types,
        )
        return cls(
            resolved_provider,
            tool_runtime=tool_runtime,
            session=session,
            context_pipeline=context_pipeline,
            context_settings=context_settings,
            retry_policy=retry_policy,
            _extension_runtime=extension_runtime,
        )

    @property
    def session_id(self) -> str:
        return self._require_session().session_id

    @property
    def session_active_leaf_id(self) -> str:
        return self._require_session().active_leaf_id

    @property
    def session_active_branch(self) -> tuple[SessionEntry, ...]:
        return self._require_session().active_branch

    @property
    def session_branches(self) -> tuple[tuple[SessionEntry, ...], ...]:
        return self._require_session().branches

    @property
    def model_contexts(self) -> tuple[ModelContext, ...]:
        """Expose immutable Context values already used or prepared by this Kernel."""

        return tuple(self._model_contexts)

    def drain_session_events(self) -> tuple[AgentSessionEvent, ...]:
        """Expose pending Session observations to a Host exactly once."""

        self._capture_session_events()
        events = tuple(self._session_events)
        self._session_events.clear()
        return events

    def drain_extension_events(self) -> tuple[ExtensionEvent, ...]:
        """Return Extension registration and dispatch facts exactly once."""

        return self._extension_runtime.drain_events()

    def fork_session(self, entry_id: str) -> None:
        self._require_session().fork(entry_id)
        self._capture_session_events()

    def close_session(self) -> None:
        self._require_session().close()

    def append_extension_entry(self, kind: str, payload: Mapping[str, object]) -> SessionEntry:
        """Append through the registered custom SessionEntry validation seam."""

        entry = self._require_session().append_custom(kind, payload)
        self._capture_session_events()
        return entry

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("This AgentKernel does not own a Session.")
        return self._session

    def _capture_session_events(self) -> None:
        """Dispatch every committed Session event without rewriting its operation.

        Session Hooks are post-commit observers. Their failures remain visible on
        the separate ExtensionEvent stream but cannot roll back or fail an already
        authoritative Session mutation.
        """

        if self._session is None:
            return
        events = self._session.drain_events()
        self._session_events.extend(events)
        for event in events:
            try:
                self._extension_runtime.observe_runtime_event(event)
            except ExtensionError:
                # The canonical Session mutation is already complete. Its event is
                # preserved for the Host and the separate ExtensionEvent is explicit.
                continue

    @staticmethod
    def _resolve_provider(
        provider: ModelProvider | str,
        extension_runtime: ExtensionRuntime,
    ) -> ModelProvider:
        return (
            extension_runtime.resolve_provider(provider) if isinstance(provider, str) else provider
        )

    @staticmethod
    def _validate_extension_tools(
        extension_runtime: ExtensionRuntime,
        tool_runtime: ToolRuntime | None,
    ) -> None:
        if not extension_runtime.tools:
            return
        if tool_runtime is None:
            raise ExtensionRegistrationError("Extension Tools require an explicit ToolRuntime")
        try:
            tool_runtime.validate_registration(extension_runtime.tools)
        except (TypeError, ValueError) as exc:
            raise ExtensionRegistrationError(str(exc)) from exc

    def create_run(
        self,
        prompt: str,
        *,
        permission_mode: PermissionMode | str = PermissionMode.AUTO,
    ) -> AgentRun:
        """Start one Agent Run for the supplied user input."""

        try:
            selected_permission_mode = PermissionMode(permission_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Permission Mode: {permission_mode!r}") from exc
        run_id = f"run-{next(self._run_numbers)}"
        input_error: AgentError | None = None
        try:
            prompt = self._extension_runtime.transform_input(prompt)
        except ExtensionBlockedError as exc:
            input_error = AgentError(
                "extension_input_blocked",
                f"{exc.code}: {exc}",
                "extension",
            )
        except ExtensionDispatchError as exc:
            input_error = AgentError("extension_input_rejected", str(exc), "extension")

        if input_error is None:
            try:
                self._extension_runtime.before_agent_start(
                    BeforeAgentStartHookInput(
                        run_id,
                        prompt,
                        None if self._session is None else self._session.session_id,
                    )
                )
            except ExtensionBlockedError as exc:
                input_error = AgentError(
                    "extension_agent_start_blocked",
                    f"{exc.code}: {exc}",
                    "extension",
                )
            except ExtensionDispatchError as exc:
                input_error = AgentError(
                    "extension_agent_start_rejected",
                    str(exc),
                    "extension",
                )

        first_request: ProviderRequest | None = None
        context_error: AgentError | None = None
        if input_error is None:
            try:
                first_request = self._build_context((UserMessage(text=prompt),), run_id=run_id)
                if self._session is not None:
                    self._session.record_user_message(prompt, run_id=run_id)
            except ContextConstructionError as exc:
                context_error = self._record_context_failure(exc, run_id=run_id)
        initial_events = () if self._session is None else self.drain_session_events()
        return AgentRun(
            run_id,
            (
                lambda control: (
                    self._extension_input_failure_events(run_id, input_error)
                    if input_error is not None
                    else self._agent_events(
                        control=control,
                        run_id=run_id,
                        prompt=prompt,
                        first_request=first_request,
                        context_error=context_error,
                        permission_mode=selected_permission_mode,
                    )
                )
            ),
            session=self._session,
            initial_events=initial_events,
            event_observer=self._extension_runtime.observe_runtime_event,
            settled_observer=self._settle_extensions,
            permission_mode=selected_permission_mode,
        )

    async def _extension_input_failure_events(
        self,
        run_id: str,
        error: AgentError,
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(kind=AgentEventKind.ERROR, run_id=run_id, error=error)

    async def _agent_events(
        self,
        *,
        control: RunControl,
        run_id: str,
        prompt: str,
        first_request: ProviderRequest | None,
        context_error: AgentError | None,
        permission_mode: PermissionMode,
    ) -> AsyncIterator[AgentEvent | AgentSessionEvent]:
        yield AgentEvent(kind=AgentEventKind.AGENT_START, run_id=run_id)
        history: list[ModelMessage] = [UserMessage(text=prompt)]
        next_injected: tuple[ModelMessage, ...] = (UserMessage(text=prompt),)
        next_active_branch: tuple[SessionEntry, ...] | None = None

        for turn_number in range(1, 21):
            turn_id = f"{run_id}-turn-{turn_number}"
            message_id = f"{turn_id}-message-1"
            accumulator = AssistantMessageAccumulator()
            message = accumulator.message
            done_seen = False

            yield AgentEvent(kind=AgentEventKind.TURN_START, run_id=run_id, turn_id=turn_id)
            if context_error is not None:
                yield AgentEvent(
                    kind=AgentEventKind.ERROR,
                    run_id=run_id,
                    turn_id=turn_id,
                    error=context_error,
                )
                yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
                yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
                return
            yield AgentEvent(
                kind=AgentEventKind.MESSAGE_START,
                run_id=run_id,
                turn_id=turn_id,
                message_id=message_id,
                message=message,
            )
            if turn_number == 1:
                if first_request is None:  # pragma: no cover - guarded by context_error
                    raise RuntimeError("First Provider request was not prepared.")
                request = first_request
            else:
                try:
                    injected = next_injected if self._session is not None else tuple(history)
                    request = self._build_context(
                        injected,
                        run_id=run_id,
                        pending_messages=control.pending_messages(),
                        active_branch=next_active_branch,
                    )
                    next_active_branch = None
                except ContextConstructionError as exc:
                    context_failure = self._record_context_failure(exc, run_id=run_id)
                    yield AgentEvent(
                        kind=AgentEventKind.ERROR,
                        run_id=run_id,
                        turn_id=turn_id,
                        message_id=message_id,
                        message=message,
                        error=context_failure,
                    )
                    yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
                    yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
                    return
            failure: ProviderError | None = None
            failure_source: Literal["provider", "extension"] = "provider"
            try:
                authoritative_request = self._extension_runtime.transform_provider_request(
                    request,
                    max_characters=self._context_settings.max_characters,
                )
            except ExtensionBlockedError as exc:
                failure_source = "extension"
                failure = ProviderError(
                    code="extension_provider_blocked",
                    message=f"{exc.code}: {exc}",
                )
            except ExtensionDispatchError as exc:
                failure_source = "extension"
                failure = ProviderError(
                    code="extension_provider_rejected",
                    message=str(exc),
                )
            if failure is not None:
                for failure_event in _provider_failure_events(
                    run_id=run_id,
                    turn_id=turn_id,
                    message_id=message_id,
                    message=message,
                    provider_error=failure,
                    source=failure_source,
                ):
                    yield failure_event
                return

            for attempt in range(1, self._retry_policy.max_attempts + 1):
                accumulator = AssistantMessageAccumulator()
                raw_accumulator = AssistantMessageAccumulator()
                message = accumulator.message
                done_seen = False
                failure = None
                failure_source = "provider"
                transformed_by: list[str] = []
                tool_transformers: dict[int, list[str]] = {}
                try:
                    provider_stream_candidate = await _isolated_provider_factory(
                        self._provider,
                        copy.deepcopy(authoritative_request),
                    )
                    if inspect.isawaitable(provider_stream_candidate):
                        dispose_awaitable(provider_stream_candidate)
                        raise TypeError("Provider.stream must return an async iterator")
                    if not isinstance(provider_stream_candidate, AsyncIterator):
                        raise TypeError("Provider.stream must return an async iterator")
                    provider_stream = provider_stream_candidate
                    close_provider_stream = getattr(provider_stream, "aclose", None)
                    try:
                        async for raw_provider_event in _isolated_provider_events(provider_stream):
                            # Validate the provider's own stream independently so a
                            # later raw fault is never attributed to an earlier,
                            # harmless Extension transform.
                            raw_provider_event = validate_provider_stream_event(raw_provider_event)
                            raw_accumulator.apply(raw_provider_event)
                            provider_event, event_transformers = (
                                self._extension_runtime.transform_provider_response(
                                    authoritative_request,
                                    raw_provider_event,
                                    attempt=attempt,
                                )
                            )
                            transformed_by.extend(event_transformers)
                            if isinstance(
                                provider_event,
                                (
                                    ProviderToolCallStart,
                                    ProviderToolCallDelta,
                                    ProviderToolCallEnd,
                                ),
                            ):
                                tool_transformers.setdefault(provider_event.index, []).extend(
                                    event_transformers
                                )
                            try:
                                message = accumulator.apply(provider_event)
                            except Exception as exc:
                                if transformed_by:
                                    if isinstance(
                                        provider_event,
                                        (
                                            ProviderToolCallStart,
                                            ProviderToolCallDelta,
                                            ProviderToolCallEnd,
                                        ),
                                    ):
                                        relevant = tool_transformers.get(provider_event.index, [])
                                    else:
                                        relevant = list(event_transformers or transformed_by)
                                    names = tuple(dict.fromkeys(relevant))
                                    if names:
                                        self._extension_runtime.record_provider_composition_failure(
                                            names,
                                            exc,
                                        )
                                    raise ExtensionDispatchError(
                                        "provider_response transform violated the stream "
                                        f"contract: {type(exc).__name__}: {exc}"
                                    ) from exc
                                raise
                            yield AgentEvent(
                                kind=AgentEventKind.MESSAGE_UPDATE,
                                run_id=run_id,
                                turn_id=turn_id,
                                message_id=message_id,
                                message=message,
                                provider_event=provider_event,
                            )
                            if isinstance(provider_event, ProviderDone):
                                done_seen = True
                            elif isinstance(provider_event, ProviderError):
                                failure = provider_event
                                break
                            elif isinstance(provider_event, ProviderAbort):
                                failure = ProviderError(
                                    code="provider_aborted", message=provider_event.reason
                                )
                                break
                            elif isinstance(provider_event, ProviderCancelled):
                                raise asyncio.CancelledError(provider_event.reason)
                    except BaseException:
                        if callable(close_provider_stream):
                            try:
                                await _isolated_provider_close(close_provider_stream)
                            except BaseException as close_error:
                                self._extension_runtime.record_provider_cleanup_failure(close_error)
                                # Preserve the authoritative dispatch/cancel failure.
                                pass
                        raise
                    else:
                        if callable(close_provider_stream):
                            try:
                                await _isolated_provider_close(close_provider_stream)
                            except BaseException as close_error:
                                self._extension_runtime.record_provider_cleanup_failure(close_error)
                                if failure is None:
                                    raise
                except ExtensionBlockedError as exc:
                    failure_source = "extension"
                    failure = ProviderError(
                        code="extension_provider_blocked",
                        message=f"{exc.code}: {exc}",
                    )
                except ExtensionDispatchError as exc:
                    failure_source = "extension"
                    failure = ProviderError(
                        code="extension_provider_rejected",
                        message=str(exc),
                    )
                except Exception as exc:
                    failure = ProviderError(
                        code="provider_exception",
                        message=f"{type(exc).__name__}: {exc}",
                    )

                if failure is None and not done_seen:
                    failure = ProviderError(
                        code="provider_stream_incomplete",
                        message="Provider stream ended without done or error.",
                    )
                    yield AgentEvent(
                        kind=AgentEventKind.MESSAGE_UPDATE,
                        run_id=run_id,
                        turn_id=turn_id,
                        message_id=message_id,
                        message=message,
                        provider_event=failure,
                    )

                if failure is None:
                    break
                remaining = self._retry_policy.max_attempts - attempt
                if remaining == 0 or not self._retry_policy.is_retryable(failure.code):
                    break
                retry_error = AgentError(failure.code, failure.message, "provider")
                yield AgentSessionEvent.from_provider_retry(
                    run_id,
                    attempt=attempt,
                    remaining=remaining,
                    error=retry_error,
                )
                await self._wait_for_retry(control)

            if failure is not None:
                for failure_event in _provider_failure_events(
                    run_id=run_id,
                    turn_id=turn_id,
                    message_id=message_id,
                    message=message,
                    provider_error=failure,
                    source=failure_source,
                ):
                    yield failure_event
                return

            try:
                message = self._extension_runtime.transform_message_end(
                    message,
                    run_id=run_id,
                    turn_id=turn_id,
                    message_id=message_id,
                )
            except ExtensionBlockedError as exc:
                failure_source = "extension"
                failure = ProviderError(
                    "extension_message_blocked",
                    f"{exc.code}: {exc}",
                )
            except ExtensionDispatchError as exc:
                failure_source = "extension"
                failure = ProviderError("extension_message_rejected", str(exc))
            if failure is not None:
                for failure_event in _provider_failure_events(
                    run_id=run_id,
                    turn_id=turn_id,
                    message_id=message_id,
                    message=message,
                    provider_error=failure,
                    source=failure_source,
                ):
                    yield failure_event
                return

            yield AgentEvent(
                kind=AgentEventKind.MESSAGE_END,
                run_id=run_id,
                turn_id=turn_id,
                message_id=message_id,
                message=message,
            )
            history.append(message)

            if message.tool_calls and self._tool_runtime is not None:
                executable: list[tuple[int, ToolCall]] = []
                precomputed: dict[int, ToolResult] = {}
                for index, call in enumerate(message.tool_calls):
                    try:
                        self._tool_runtime.validate_call(call)
                    except ValueError:
                        executable.append((index, call))
                        continue
                    try:
                        transformed = self._extension_runtime.transform_tool_call(
                            call,
                            run_id=run_id,
                            turn_id=turn_id,
                            validator=self._tool_runtime.validate_call,
                        )
                        executable.append((index, transformed))
                    except ExtensionBlockedError as exc:
                        precomputed[index] = ToolResult(
                            call.call_id,
                            call.tool_name,
                            "error",
                            error=ToolError(
                                "extension_blocked",
                                f"{exc.code}: {exc}",
                            ),
                        )
                    except ExtensionDispatchError as exc:
                        precomputed[index] = ToolResult(
                            call.call_id,
                            call.tool_name,
                            "error",
                            error=ToolError("extension_rejected", str(exc)),
                        )

                permission_decisions: dict[str, PermissionDecision] = {}
                permission_executable: list[tuple[int, ToolCall]] = []
                for index, call in executable:
                    permission_request = None
                    try:
                        evaluation = self._tool_runtime.evaluate_permission(
                            call,
                            permission_mode,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        try:
                            self._tool_runtime.validate_call(call)
                        except ValueError:
                            permission_executable.append((index, call))
                        else:
                            precomputed[index] = ToolResult(
                                call.call_id,
                                call.tool_name,
                                "error",
                                error=ToolError(
                                    "permission_invalid",
                                    permission_classification_error_message(exc),
                                ),
                            )
                        continue
                    if evaluation.action is PermissionAction.ASK:
                        permission_request = control.open_permission(
                            call,
                            evaluation,
                            permission_mode,
                        )
                        yield AgentSessionEvent.from_permission_request(permission_request)
                        decision = await control.wait_for_permission(permission_request)
                    else:
                        decision = make_permission_decision(
                            mode=permission_mode,
                            call=call,
                            evaluation=evaluation,
                            approved=evaluation.action is PermissionAction.ALLOW,
                            source="policy",
                        )
                    permission_decisions[call.call_id] = decision
                    if permission_request is None and self._session is not None:
                        self._session.record_permission_decision(decision, run_id=run_id)
                        for session_event in self._session.drain_events():
                            yield session_event
                    permission_executable.append((index, call))
                executable = permission_executable
                executable_calls = tuple(call for _, call in executable)
                batch_mode = self._tool_runtime.batch_mode(executable_calls)
                for _, call in executable:
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_EXECUTION_START,
                        run_id=run_id,
                        turn_id=turn_id,
                        tool_call=call,
                        batch_mode=batch_mode,
                    )
                progress_queue: asyncio.Queue[ToolProgress] = asyncio.Queue()

                batch_task = asyncio.create_task(
                    self._tool_runtime.execute_guarded_batch(
                        executable_calls,
                        control.cancel_event,
                        on_progress=partial(_put_progress, progress_queue),
                        permission_mode=permission_mode,
                        permission_decisions=permission_decisions,
                    )
                )
                try:
                    while not batch_task.done() or not progress_queue.empty():
                        if not progress_queue.empty():
                            progress = progress_queue.get_nowait()
                        else:
                            progress_task = asyncio.create_task(progress_queue.get())
                            done, _ = await asyncio.wait(
                                {batch_task, progress_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if progress_task not in done:
                                progress_task.cancel()
                                await asyncio.gather(progress_task, return_exceptions=True)
                                continue
                            progress = progress_task.result()
                        yield AgentEvent(
                            kind=AgentEventKind.TOOL_EXECUTION_UPDATE,
                            run_id=run_id,
                            turn_id=turn_id,
                            tool_progress=progress,
                            batch_mode=batch_mode,
                        )
                    batch = await batch_task
                finally:
                    if not batch_task.done():
                        batch_task.cancel()
                        await asyncio.gather(batch_task, return_exceptions=True)
                for index, result in zip(
                    (index for index, _ in executable),
                    batch.results,
                    strict=True,
                ):
                    precomputed[index] = result
                ordered_results = tuple(
                    precomputed[index] for index in range(len(message.tool_calls))
                )
                transformed_results: list[ToolResult] = []
                for result in ordered_results:
                    try:
                        transformed_results.append(
                            self._extension_runtime.transform_tool_result(
                                result,
                                run_id=run_id,
                                turn_id=turn_id,
                            )
                        )
                    except ExtensionBlockedError as exc:
                        if result.status == "cancelled":
                            # Cancellation is an authoritative execution fact. The
                            # ExtensionEvent retains the secondary block diagnostic.
                            transformed_results.append(result)
                            continue
                        transformed_results.append(
                            ToolResult(
                                result.call_id,
                                result.tool_name,
                                "error",
                                result.output,
                                ToolError("extension_blocked", f"{exc.code}: {exc}"),
                            )
                        )
                    except ExtensionDispatchError as exc:
                        if result.status == "cancelled":
                            # Rejection/failure is secondary to the original
                            # cancellation and must not rewrite its provenance.
                            transformed_results.append(result)
                            continue
                        transformed_results.append(
                            ToolResult(
                                result.call_id,
                                result.tool_name,
                                "error",
                                result.output,
                                ToolError("extension_rejected", str(exc)),
                            )
                        )
                ordered_results = tuple(transformed_results)
                executable_indexes = {call.call_id: index for index, call in executable}
                for call_id in batch.completion_order:
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_EXECUTION_END,
                        run_id=run_id,
                        turn_id=turn_id,
                        tool_result=ordered_results[executable_indexes[call_id]],
                        batch_mode=batch.mode,
                    )
                for index in sorted(set(precomputed) - set(executable_indexes.values())):
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_EXECUTION_END,
                        run_id=run_id,
                        turn_id=turn_id,
                        tool_result=ordered_results[index],
                        batch_mode=batch.mode,
                    )
                history.append(ToolResultMessage(results=ordered_results))
                tool_results = ToolResultMessage(results=ordered_results)
                next_injected = () if self._session is not None else (tool_results,)
                steering = control.drain_steering()
                if steering:
                    next_injected, next_active_branch = self._prepare_control_injection(
                        history,
                        steering,
                        prefix=next_injected,
                    )
                    async for control_event in self._inject_messages(run_id, steering):
                        yield control_event
                yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
                continue

            steering = control.drain_steering()
            if steering:
                next_injected, next_active_branch = self._prepare_control_injection(
                    history, steering
                )
                async for control_event in self._inject_messages(run_id, steering):
                    yield control_event
                yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
                continue

            follow_up = control.drain_follow_up()
            if follow_up:
                next_injected, next_active_branch = self._prepare_control_injection(
                    history, follow_up
                )
                yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
                yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
                async for control_event in self._inject_messages(run_id, follow_up):
                    yield control_event
                yield AgentEvent(kind=AgentEventKind.AGENT_START, run_id=run_id)
                continue

            yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
            yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
            return

        error = ProviderError("turn_limit", "Agent Run exceeded 20 Turns.")
        for failure_event in _provider_failure_events(
            run_id=run_id,
            turn_id=f"{run_id}-turn-20",
            message_id=f"{run_id}-turn-20-message-1",
            message=message,
            provider_error=error,
        ):
            yield failure_event

    def _build_context(
        self,
        injected_messages: tuple[ModelMessage, ...],
        *,
        run_id: str,
        pending_messages: tuple[ModelMessage, ...] = (),
        active_branch: tuple[SessionEntry, ...] | None = None,
    ) -> ProviderRequest:
        result = self._context_pipeline.build(
            ContextInput(
                settings=self._context_settings,
                active_branch=(
                    ()
                    if self._session is None
                    else self._session.active_branch
                    if active_branch is None
                    else active_branch
                ),
                active_tools=self._tool_schemas(),
                injected_messages=injected_messages,
                pending_messages=pending_messages,
            )
        )
        context = result.context
        compaction = None
        if result.compaction is not None:
            if self._session is None:  # pragma: no cover - no branch can produce a plan
                raise RuntimeError("Compaction requires a durable Session.")
            try:
                compaction = self._extension_runtime.transform_compaction(
                    result.compaction,
                    run_id=run_id,
                    session_id=self._session.session_id,
                    active_branch=tuple(entry.entry_id for entry in self._session.active_branch),
                    validator=self._session.validate_compaction,
                )
            except ExtensionBlockedError as exc:
                raise ContextConstructionError(
                    "extension_compaction_blocked",
                    f"{exc.code}: {exc}",
                    stage="compaction",
                ) from exc
            except ExtensionDispatchError as exc:
                raise ContextConstructionError(
                    "extension_compaction_rejected",
                    str(exc),
                    stage="compaction",
                ) from exc
            context = self._context_with_compaction(context, compaction)
        try:
            context = self._extension_runtime.transform_context(context)
        except ExtensionBlockedError as exc:
            raise ContextConstructionError(
                "extension_context_blocked",
                f"{exc.code}: {exc}",
                stage="context",
            ) from exc
        except ExtensionDispatchError as exc:
            raise ContextConstructionError(
                "extension_context_rejected",
                str(exc),
                stage="context",
            ) from exc
        if compaction is not None:
            self._require_session().record_compaction(compaction, run_id=run_id)
        self._model_contexts.append(context)
        return context.provider_request

    @staticmethod
    def _context_with_compaction(
        context: ModelContext,
        compaction: CompactionPlan,
    ) -> ModelContext:
        request = context.provider_request
        if not request.messages or not isinstance(request.messages[0], BranchSummaryMessage):
            raise ContextConstructionError(
                "extension_compaction_rejected",
                "Compaction transform has no canonical summary message to replace.",
                stage="compaction",
            )
        transformed_request = ProviderRequest(
            messages=(BranchSummaryMessage(text=compaction.summary), *request.messages[1:]),
            tools=request.tools,
            system_prompt=request.system_prompt,
            tool_guidelines=request.tool_guidelines,
            project_context=request.project_context,
        )
        estimated = estimate_provider_request_characters(transformed_request)
        if estimated > context.max_characters:
            raise ContextConstructionError(
                "extension_compaction_rejected",
                "Transformed compaction summary exceeds the canonical Context budget.",
                stage="compaction",
            )
        return ModelContext(
            provider_request=transformed_request,
            estimated_characters=estimated,
            max_characters=context.max_characters,
            assembly_order=context.assembly_order,
        )

    async def _inject_messages(
        self, run_id: str, messages: tuple[PendingMessage, ...]
    ) -> AsyncIterator[AgentSessionEvent]:
        for message in messages:
            yield AgentSessionEvent.from_pending_message(
                AgentSessionEventKind.MESSAGE_INJECTED,
                run_id,
                message,
                0,
            )
            if self._session is not None:
                self._session.record_user_message(message.text, run_id=run_id)
                for event in self._session.drain_events():
                    yield event

    def _prepare_control_injection(
        self,
        history: list[ModelMessage],
        messages: tuple[PendingMessage, ...],
        *,
        prefix: tuple[ModelMessage, ...] = (),
    ) -> tuple[tuple[ModelMessage, ...], tuple[SessionEntry, ...] | None]:
        branch_before_injection = None if self._session is None else self._session.active_branch
        injected_users = tuple(UserMessage(text=message.text) for message in messages)
        history.extend(injected_users)
        return (*prefix, *injected_users), branch_before_injection

    async def _wait_for_retry(self, control: RunControl) -> None:
        delay = self._retry_policy.delay_seconds
        if delay == 0:
            await asyncio.sleep(0)
            return
        delay_task = asyncio.create_task(asyncio.sleep(delay))
        cancel_task = asyncio.create_task(control.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {delay_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                raise asyncio.CancelledError
        finally:
            for task in (delay_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(delay_task, cancel_task, return_exceptions=True)

    def _record_context_failure(
        self, error: ContextConstructionError, *, run_id: str
    ) -> AgentError:
        normalized = AgentError(
            code=error.code,
            message=str(error),
            source="extension" if error.code.startswith("extension_") else "kernel",
        )
        if self._session is not None:
            self._session.record_context_failure(
                normalized,
                stage=error.stage,
                run_id=run_id,
            )
        return normalized

    def _settle_extensions(
        self,
        result: AgentRunResult,
    ) -> tuple[AgentSessionEvent, ...]:
        def validate(kind: str, payload: Mapping[str, object]) -> None:
            if self._session is None:
                raise ExtensionDispatchError(
                    "agent_settled SessionEntry supplement requires a durable Session"
                )
            self._session.validate_custom_entry(kind, payload)

        try:
            drafts = self._extension_runtime.settle_agent(result, validator=validate)
        except ExtensionDispatchError:
            return ()
        if not drafts:
            return ()
        if self._session is None:
            return ()
        try:
            self._session.append_custom_many(
                tuple((draft.kind, draft.payload) for draft in drafts),
                run_id=result.run_id,
            )
        except Exception as exc:
            self._extension_runtime.record_settlement_failure(exc)
            return ()
        return self._session.drain_events()

    def _tool_schemas(self) -> tuple[dict[str, object], ...]:
        if self._tool_runtime is None:
            return ()
        return tuple(
            {
                "name": spec.name,
                "description": spec.description,
                "schema": spec.schema,
                "mode": spec.mode,
            }
            for spec in self._tool_runtime.schemas
        )
