from __future__ import annotations

import asyncio
from collections.abc import Sequence

from coding_agent import (
    AgentKernel,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    FakeProvider,
    ProviderDone,
    ProviderError,
    ProviderTextDelta,
)


async def _collect(
    provider: FakeProvider,
) -> tuple[list[AgentSessionEvent], AgentRunResult]:
    run = AgentKernel(provider).create_run("test input")
    assert run.state is AgentRunState.ACTIVE
    events = [event async for event in run]
    return events, await run.result()


def _kinds(events: Sequence[AgentSessionEvent]) -> list[AgentSessionEventKind]:
    return [event.kind for event in events]


def test_streamed_run_preserves_layering_and_authoritative_message() -> None:
    events, result = asyncio.run(_collect(FakeProvider.streamed_run()))

    assert _kinds(events) == [
        AgentSessionEventKind.AGENT_START,
        AgentSessionEventKind.TURN_START,
        AgentSessionEventKind.MESSAGE_START,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_END,
        AgentSessionEventKind.TURN_END,
        AgentSessionEventKind.AGENT_END,
        AgentSessionEventKind.RUN_SETTLED,
    ]

    updates = [event for event in events if event.kind is AgentSessionEventKind.MESSAGE_UPDATE]
    assert all(event.agent_event is not None for event in updates)
    assert all(event.message is not None for event in updates)
    assert all(event.provider_event is not None for event in updates)
    assert isinstance(updates[2].provider_event, ProviderTextDelta)
    assert updates[2].message is not None
    assert updates[2].message.text == "Hello"
    assert isinstance(updates[-1].provider_event, ProviderDone)

    message_end = [event for event in events if event.kind is AgentSessionEventKind.MESSAGE_END]
    assert len(message_end) == 1
    assert result.state is AgentRunState.SETTLED
    assert result.message == message_end[0].message
    assert result.message is not None
    assert result.message.text == "Hello from the Fake Provider."
    assert result.message.thinking == "Plan the response. Then answer."
    assert events[-1].result == result


def test_provider_error_is_structured_and_has_one_terminal_state() -> None:
    events, result = asyncio.run(_collect(FakeProvider.provider_error()))

    assert _kinds(events) == [
        AgentSessionEventKind.AGENT_START,
        AgentSessionEventKind.TURN_START,
        AgentSessionEventKind.MESSAGE_START,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.MESSAGE_UPDATE,
        AgentSessionEventKind.ERROR,
        AgentSessionEventKind.TURN_END,
        AgentSessionEventKind.AGENT_END,
        AgentSessionEventKind.RUN_FAILED,
    ]
    assert isinstance(events[5].provider_event, ProviderError)
    assert events[6].error is not None
    assert events[6].error.code == "scripted_provider_error"
    assert all(event.kind is not AgentSessionEventKind.MESSAGE_END for event in events)
    assert sum(event.result is not None for event in events) == 1
    assert result.state is AgentRunState.FAILED
    assert result.message is None
    assert result.error is not None
    assert result.error.source == "provider"
    assert events[-1].result == result


def test_stream_without_done_becomes_a_protocol_failure() -> None:
    events, result = asyncio.run(_collect(FakeProvider([])))

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "provider_stream_incomplete"
    assert events[-2].kind is AgentSessionEventKind.AGENT_END
    assert events[-1].kind is AgentSessionEventKind.RUN_FAILED


def test_run_state_space_names_all_required_states() -> None:
    assert {state.value for state in AgentRunState} == {
        "active",
        "settled",
        "cancelled",
        "failed",
    }
    assert AgentRunState.ACTIVE.terminal is False
    assert all(state.terminal for state in AgentRunState if state is not AgentRunState.ACTIVE)
