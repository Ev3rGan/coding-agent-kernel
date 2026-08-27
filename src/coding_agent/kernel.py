"""Headless AgentKernel orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from functools import partial
from itertools import count

from coding_agent.context import (
    ContextConstructionError,
    ContextInput,
    ContextPipeline,
    ContextSettings,
    ModelContext,
)
from coding_agent.control import RetryPolicy, RunControl
from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    AssistantMessageAccumulator,
    PendingMessage,
    ProviderAbort,
    ProviderCancelled,
    ProviderDone,
    ProviderError,
    ToolCall,
    ToolError,
    ToolProgress,
    ToolResult,
)
from coding_agent.extensions import (
    Extension,
    ExtensionEvent,
    ExtensionRuntime,
    HookInput,
    HookName,
)
from coding_agent.provider import (
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


def _provider_failure_events(
    *,
    run_id: str,
    turn_id: str,
    message_id: str,
    message: AssistantMessage,
    provider_error: ProviderError,
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
                source="provider",
            ),
        ),
        AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id),
        AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id),
    )


class AgentKernel:
    """Create AgentRun handles while keeping AgentLoop state behind one seam."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        tool_runtime: ToolRuntime | None = None,
        session: Session | None = None,
        context_pipeline: ContextPipeline | None = None,
        context_settings: ContextSettings | None = None,
        retry_policy: RetryPolicy | None = None,
        extensions: Sequence[Extension] = (),
    ) -> None:
        self._provider = provider
        self._tool_runtime = tool_runtime
        self._session = session
        self._context_pipeline = context_pipeline or ContextPipeline()
        self._context_settings = context_settings or ContextSettings()
        self._retry_policy = retry_policy or RetryPolicy()
        self._model_contexts: list[ModelContext] = []
        self._run_numbers = count(1)
        self._extension_runtime = ExtensionRuntime()
        self._extension_context_needed = False
        for extension in extensions:
            self._register_extension(extension)

    def _register_extension(self, extension: Extension) -> None:
        """Register one Extension and surface its contributed Tools on the runtime."""
        self._extension_runtime.register(extension)
        self._extension_context_needed = self._extension_runtime.has_handler(HookName.CONTEXT)
        if self._tool_runtime is not None:
            for declared in self._extension_runtime.declared_tools_of(extension.name):
                self._tool_runtime.register(declared.tool)
                if declared.enabled:
                    self._tool_runtime.enable(declared.tool.spec.name)

    @property
    def extension_events(self) -> tuple[ExtensionEvent, ...]:
        """Expose every dispatched, independently contracted Extension observation."""
        return self._extension_runtime.events

    @property
    def extension_names(self) -> tuple[str, ...]:
        return self._extension_runtime.registered_extensions

    @classmethod
    def with_new_session(
        cls,
        provider: ModelProvider,
        store: SessionStore,
        *,
        configuration: Mapping[str, object],
        session_id: str | None = None,
        entry_id_factory: Callable[[], str] | None = None,
        tool_runtime: ToolRuntime | None = None,
        context_pipeline: ContextPipeline | None = None,
        context_settings: ContextSettings | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> AgentKernel:
        """Create a Kernel that owns a new durable Session."""

        session = Session.create(
            store,
            configuration=configuration,
            session_id=session_id,
            entry_id_factory=entry_id_factory,
        )
        return cls(
            provider,
            tool_runtime=tool_runtime,
            session=session,
            context_pipeline=context_pipeline,
            context_settings=context_settings,
            retry_policy=retry_policy,
        )

    @classmethod
    def with_resumed_session(
        cls,
        provider: ModelProvider,
        store: SessionStore,
        session_id: str,
        *,
        entry_id_factory: Callable[[], str] | None = None,
        tool_runtime: ToolRuntime | None = None,
        context_pipeline: ContextPipeline | None = None,
        context_settings: ContextSettings | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> AgentKernel:
        """Create a Kernel after validating and resuming persisted Session history."""

        session = Session.resume(
            store,
            session_id,
            entry_id_factory=entry_id_factory,
        )
        return cls(
            provider,
            tool_runtime=tool_runtime,
            session=session,
            context_pipeline=context_pipeline,
            context_settings=context_settings,
            retry_policy=retry_policy,
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

        return self._require_session().drain_events()

    def fork_session(self, entry_id: str) -> None:
        self._require_session().fork(entry_id)

    def close_session(self) -> None:
        self._require_session().close()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("This AgentKernel does not own a Session.")
        return self._session

    def create_run(self, prompt: str) -> AgentRun:
        """Start one Agent Run for the supplied user input."""

        run_id = f"run-{next(self._run_numbers)}"
        first_request: ProviderRequest | None = None
        context_error: AgentError | None = None
        try:
            first_request = self._build_context_sync(
                (UserMessage(text=prompt),), run_id=run_id
            )
            if self._session is not None:
                self._session.record_user_message(prompt, run_id=run_id)
        except ContextConstructionError as exc:
            context_error = self._record_context_failure(exc, run_id=run_id)
        initial_events = () if self._session is None else self._session.drain_events()
        return AgentRun(
            run_id,
            lambda control: self._agent_events(
                control=control,
                run_id=run_id,
                prompt=prompt,
                first_request=first_request,
                context_error=context_error,
            ),
            session=self._session,
            initial_events=initial_events,
        )

    async def _agent_events(
        self,
        *,
        control: RunControl,
        run_id: str,
        prompt: str,
        first_request: ProviderRequest | None,
        context_error: AgentError | None,
    ) -> AsyncIterator[AgentEvent | AgentSessionEvent]:
        await self._run_hook(HookName.BEFORE_AGENT_START, run_id=run_id)
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
                await self._run_hook(HookName.AGENT_SETTLED, run_id=run_id)
                yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
                return
            yield AgentEvent(
                kind=AgentEventKind.MESSAGE_START,
                run_id=run_id,
                turn_id=turn_id,
                message_id=message_id,
                message=message,
            )
            try:
                if turn_number == 1 and first_request is not None:
                    if self._extension_context_needed:
                        request = await self._build_context(
                            (UserMessage(text=prompt),),
                            run_id=run_id,
                        )
                    else:
                        request = first_request
                else:
                    injected = next_injected if self._session is not None else tuple(history)
                    request = await self._build_context(
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
                await self._run_hook(HookName.AGENT_SETTLED, run_id=run_id)
                yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
                return
            failure: ProviderError | None = None
            for attempt in range(1, self._retry_policy.max_attempts + 1):
                accumulator = AssistantMessageAccumulator()
                message = accumulator.message
                done_seen = False
                failure = None
                try:
                    async for provider_event in self._provider.stream(request):
                        message = accumulator.apply(provider_event)
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
                ):
                    yield failure_event
                await self._run_hook(HookName.AGENT_SETTLED, run_id=run_id)
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
                resolved, blocked = await self._resolve_tool_calls(
                    message.tool_calls, run_id=run_id, turn_id=turn_id
                )
                blocked_results: list[ToolResult] = []
                for call, reason in blocked:
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_EXECUTION_START,
                        run_id=run_id,
                        turn_id=turn_id,
                        tool_call=call,
                        batch_mode="sequential",
                    )
                    result = ToolResult(
                        call.call_id,
                        call.tool_name,
                        "error",
                        None,
                        ToolError("extension_blocked", reason),
                    )
                    blocked_results.append(result)
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_EXECUTION_END,
                        run_id=run_id,
                        turn_id=turn_id,
                        tool_result=result,
                        batch_mode="sequential",
                    )
                if not resolved:
                    results = tuple(blocked_results)
                else:
                    batch_mode = self._tool_runtime.batch_mode(resolved)
                    for call in resolved:
                        yield AgentEvent(
                            kind=AgentEventKind.TOOL_EXECUTION_START,
                            run_id=run_id,
                            turn_id=turn_id,
                            tool_call=call,
                            batch_mode=batch_mode,
                        )
                    progress_queue: asyncio.Queue[ToolProgress] = asyncio.Queue()
                    batch_task = asyncio.create_task(
                        self._tool_runtime.execute_batch(
                            resolved,
                            control.cancel_event,
                            on_progress=partial(_put_progress, progress_queue),
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
                                    await asyncio.gather(
                                        progress_task, return_exceptions=True
                                    )
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
                    results_by_id = {result.call_id: result for result in batch.results}
                    for call_id in batch.completion_order:
                        result = results_by_id[call_id]
                        yield AgentEvent(
                            kind=AgentEventKind.TOOL_EXECUTION_END,
                            run_id=run_id,
                            turn_id=turn_id,
                            tool_result=result,
                            batch_mode=batch.mode,
                        )
                    results = (*blocked_results, *batch.results)
                tool_results = ToolResultMessage(results=results)
                history.append(tool_results)
                next_injected = (tool_results,)
                steering = control.drain_steering()
                if steering:
                    next_injected, next_active_branch = self._prepare_control_injection(
                        history,
                        steering,
                        prefix=(tool_results,),
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
            await self._run_hook(HookName.AGENT_SETTLED, run_id=run_id)
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
        await self._run_hook(HookName.AGENT_SETTLED, run_id=run_id)

    def _build_context_sync(
        self,
        injected_messages: tuple[ModelMessage, ...],
        *,
        run_id: str,
    ) -> ProviderRequest:
        """Synchronously build one Provider request without Extension Hooks.

        This is used by create_run to fail fast on Context construction errors before
        an Agent Run starts, matching the deterministic pre-Provider failure surface.
        """
        active = () if self._session is None else self._session.active_branch
        result = self._context_pipeline.build(
            ContextInput(
                settings=self._context_settings,
                active_branch=active,
                active_tools=self._tool_schemas(),
                injected_messages=injected_messages,
            )
        )
        if result.compaction is not None:
            if self._session is None:  # pragma: no cover - no branch can produce a plan
                raise RuntimeError("Compaction requires a durable Session.")
            self._session.record_compaction(result.compaction, run_id=run_id)
        request = result.context.provider_request
        self._model_contexts.append(result.context)
        return request

    async def _build_context(
        self,
        injected_messages: tuple[ModelMessage, ...],
        *,
        run_id: str,
        pending_messages: tuple[ModelMessage, ...] = (),
        active_branch: tuple[SessionEntry, ...] | None = None,
    ) -> ProviderRequest:
        supplement, _ = await self._run_hook(
            HookName.CONTEXT,
            run_id=run_id,
            context=None,
        )
        settings = self._context_settings
        context_lines = supplement.get("context_lines")
        if isinstance(context_lines, tuple) and context_lines:
            settings = ContextSettings(
                system_prompt=settings.system_prompt,
                tool_guidelines=settings.tool_guidelines,
                project_context=(*settings.project_context, *context_lines),
                max_characters=settings.max_characters,
            )
        active = (
            ()
            if self._session is None
            else self._session.active_branch
            if active_branch is None
            else active_branch
        )
        result = self._context_pipeline.build(
            ContextInput(
                settings=settings,
                active_branch=active,
                active_tools=self._tool_schemas(),
                injected_messages=injected_messages,
                pending_messages=pending_messages,
            )
        )
        if result.compaction is not None:
            if self._session is None:  # pragma: no cover - no branch can produce a plan
                raise RuntimeError("Compaction requires a durable Session.")
            self._session.record_compaction(result.compaction, run_id=run_id)
        request = result.context.provider_request
        self._model_contexts.append(result.context)
        maybe_block, _ = await self._run_hook(
            HookName.PROVIDER_REQUEST,
            run_id=run_id,
            request=request,
        )
        if maybe_block.get("blocked"):
            raise ContextConstructionError(
                "provider_request_blocked",
                f"Provider request blocked by an Extension: {maybe_block.get('reason')}",
                stage="provider-request",
            )
        return request

    async def _resolve_tool_calls(
        self,
        calls: tuple[ToolCall, ...],
        *,
        run_id: str,
        turn_id: str,
    ) -> tuple[tuple[ToolCall, ...], list[tuple[ToolCall, str]]]:
        """Apply the tool_call Hook to every call, returning allowed and blocked."""
        resolved: list[ToolCall] = []
        blocked: list[tuple[ToolCall, str]] = []
        for call in calls:
            decision, _ = await self._run_hook(
                HookName.TOOL_CALL,
                run_id=run_id,
                turn_id=turn_id,
                tool_call=call,
            )
            if decision.get("blocked"):
                blocked.append((call, str(decision.get("reason", "extension policy"))))
                continue
            transformed = decision.get("tool_call")
            resolved.append(transformed if isinstance(transformed, ToolCall) else call)
        return tuple(resolved), blocked

    async def _run_hook(
        self,
        hook: HookName,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        tool_call: ToolCall | None = None,
        tool_result: ToolResult | None = None,
        context: ModelContext | None = None,
        request: ProviderRequest | None = None,
        message: AssistantMessage | None = None,
    ) -> tuple[dict[str, object], tuple[ExtensionEvent, ...]]:
        """Dispatch one fixed Hook through the Extension runtime."""
        if not self._extension_runtime.registered_extensions:
            return {"hook": hook.value}, ()
        return await self._extension_runtime.run_once(
            hook,
            HookInput(
                hook=hook,
                run_id=run_id,
                turn_id=turn_id,
                tool_call=tool_call,
                tool_result=tool_result,
                context=context,
                request=request,
                message=message,
            ),
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
        normalized = AgentError(code=error.code, message=str(error), source="kernel")
        if self._session is not None:
            self._session.record_context_failure(
                normalized,
                stage=error.stage,
                run_id=run_id,
            )
        return normalized

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
