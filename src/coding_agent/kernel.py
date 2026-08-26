"""Headless AgentKernel orchestration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from itertools import count

from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AssistantMessage,
    ProviderDone,
    ProviderError,
)
from coding_agent.provider import ModelProvider
from coding_agent.run import AgentRun


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

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        self._run_numbers = count(1)

    def create_run(self, prompt: str) -> AgentRun:
        """Start one Agent Run for the supplied user input."""

        run_id = f"run-{next(self._run_numbers)}"
        return AgentRun(run_id, self._agent_events(run_id=run_id, prompt=prompt))

    async def _agent_events(self, *, run_id: str, prompt: str) -> AsyncIterator[AgentEvent]:
        turn_id = f"{run_id}-turn-1"
        message_id = f"{turn_id}-message-1"
        message = AssistantMessage()

        yield AgentEvent(kind=AgentEventKind.AGENT_START, run_id=run_id)
        yield AgentEvent(kind=AgentEventKind.TURN_START, run_id=run_id, turn_id=turn_id)
        yield AgentEvent(
            kind=AgentEventKind.MESSAGE_START,
            run_id=run_id,
            turn_id=turn_id,
            message_id=message_id,
            message=message,
        )

        try:
            async for provider_event in self._provider.stream(prompt):
                message = message.apply(provider_event)
                yield AgentEvent(
                    kind=AgentEventKind.MESSAGE_UPDATE,
                    run_id=run_id,
                    turn_id=turn_id,
                    message_id=message_id,
                    message=message,
                    provider_event=provider_event,
                )

                if isinstance(provider_event, ProviderDone):
                    yield AgentEvent(
                        kind=AgentEventKind.MESSAGE_END,
                        run_id=run_id,
                        turn_id=turn_id,
                        message_id=message_id,
                        message=message,
                    )
                    yield AgentEvent(
                        kind=AgentEventKind.TURN_END,
                        run_id=run_id,
                        turn_id=turn_id,
                    )
                    yield AgentEvent(kind=AgentEventKind.AGENT_END, run_id=run_id)
                    return

                if isinstance(provider_event, ProviderError):
                    for failure_event in _provider_failure_events(
                        run_id=run_id,
                        turn_id=turn_id,
                        message_id=message_id,
                        message=message,
                        provider_error=provider_event,
                    ):
                        yield failure_event
                    return
        except Exception as exc:
            synthetic_provider_error = ProviderError(
                code="provider_exception",
                message=f"{type(exc).__name__}: {exc}",
            )
        else:
            synthetic_provider_error = ProviderError(
                code="provider_stream_incomplete",
                message="Provider stream ended without done or error.",
            )

        yield AgentEvent(
            kind=AgentEventKind.MESSAGE_UPDATE,
            run_id=run_id,
            turn_id=turn_id,
            message_id=message_id,
            message=message,
            provider_event=synthetic_provider_error,
        )
        for failure_event in _provider_failure_events(
            run_id=run_id,
            turn_id=turn_id,
            message_id=message_id,
            message=message,
            provider_error=synthetic_provider_error,
        ):
            yield failure_event
