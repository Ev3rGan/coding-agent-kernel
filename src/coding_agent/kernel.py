"""Headless AgentKernel orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from functools import partial
from itertools import count

from coding_agent.context import (
    ContextConstructionError,
    ContextInput,
    ContextPipeline,
    ContextSettings,
    ModelContext,
)
from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentSessionEvent,
    AssistantMessage,
    AssistantMessageAccumulator,
    ProviderAbort,
    ProviderCancelled,
    ProviderDone,
    ProviderError,
    ToolProgress,
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
    ) -> None:
        self._provider = provider
        self._tool_runtime = tool_runtime
        self._session = session
        self._context_pipeline = context_pipeline or ContextPipeline()
        self._context_settings = context_settings or ContextSettings()
        self._model_contexts: list[ModelContext] = []
        self._run_numbers = count(1)

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
            first_request = self._build_context((UserMessage(text=prompt),), run_id=run_id)
            if self._session is not None:
                self._session.record_user_message(prompt, run_id=run_id)
        except ContextConstructionError as exc:
            context_error = self._record_context_failure(exc, run_id=run_id)
        initial_events = () if self._session is None else self._session.drain_events()
        return AgentRun(
            run_id,
            self._agent_events(
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
        run_id: str,
        prompt: str,
        first_request: ProviderRequest | None,
        context_error: AgentError | None,
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(kind=AgentEventKind.AGENT_START, run_id=run_id)
        history: list[ModelMessage] = [UserMessage(text=prompt)]
        next_injected: tuple[ModelMessage, ...] = (UserMessage(text=prompt),)

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
                    request = self._build_context(injected, run_id=run_id)
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

            if failure is not None:
                for failure_event in _provider_failure_events(
                    run_id=run_id,
                    turn_id=turn_id,
                    message_id=message_id,
                    message=message,
                    provider_error=failure,
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
                batch_mode = self._tool_runtime.batch_mode(message.tool_calls)
                for call in message.tool_calls:
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
                        message.tool_calls,
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
                history.append(ToolResultMessage(results=batch.results))
                next_injected = (ToolResultMessage(results=batch.results),)
                yield AgentEvent(kind=AgentEventKind.TURN_END, run_id=run_id, turn_id=turn_id)
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
    ) -> ProviderRequest:
        result = self._context_pipeline.build(
            ContextInput(
                settings=self._context_settings,
                active_branch=(() if self._session is None else self._session.active_branch),
                active_tools=self._tool_schemas(),
                injected_messages=injected_messages,
            )
        )
        if result.compaction is not None:
            if self._session is None:  # pragma: no cover - no branch can produce a plan
                raise RuntimeError("Compaction requires a durable Session.")
            self._session.record_compaction(result.compaction, run_id=run_id)
        self._model_contexts.append(result.context)
        return result.context.provider_request

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
