"""Headless AgentKernel orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from functools import partial
from itertools import count

from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
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

    def __init__(self, provider: ModelProvider, *, tool_runtime: ToolRuntime | None = None) -> None:
        self._provider = provider
        self._tool_runtime = tool_runtime
        self._run_numbers = count(1)

    def create_run(self, prompt: str) -> AgentRun:
        """Start one Agent Run for the supplied user input."""

        run_id = f"run-{next(self._run_numbers)}"
        return AgentRun(run_id, self._agent_events(run_id=run_id, prompt=prompt))

    async def _agent_events(self, *, run_id: str, prompt: str) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(kind=AgentEventKind.AGENT_START, run_id=run_id)
        history: list[ModelMessage] = [UserMessage(text=prompt)]

        for turn_number in range(1, 21):
            turn_id = f"{run_id}-turn-{turn_number}"
            message_id = f"{turn_id}-message-1"
            accumulator = AssistantMessageAccumulator()
            message = accumulator.message
            done_seen = False

            yield AgentEvent(kind=AgentEventKind.TURN_START, run_id=run_id, turn_id=turn_id)
            yield AgentEvent(
                kind=AgentEventKind.MESSAGE_START,
                run_id=run_id,
                turn_id=turn_id,
                message_id=message_id,
                message=message,
            )
            request = ProviderRequest(tuple(history), self._tool_schemas())
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
