from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from coding_agent import (
    AgentKernel,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    FakeProvider,
    InMemorySessionStore,
    LocalCodingEnvironment,
    ProviderDone,
    ProviderError,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ToolRuntime,
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


def test_host_can_disable_turn_limit_when_external_timeout_owns_budget(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("ready\n", encoding="utf-8")

    def tool_turn(number: int) -> tuple[ProviderStreamEvent, ...]:
        return (
            ProviderToolCallStart(index=0),
            ProviderToolCallDelta(
                index=0,
                call_id_delta=f"read-{number}",
                tool_name_delta="read",
                arguments_delta=json.dumps({"path": "value.txt"}),
            ),
            ProviderToolCallEnd(index=0),
            ProviderDone(stop_reason="tool_use"),
        )

    provider = FakeProvider(
        tuple(tool_turn(number) for number in range(1, 22))
        + ((ProviderTextDelta("complete"), ProviderDone()),)
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("read until complete", max_turns=None)
        return [event async for event in run], await run.result()

    events, result = asyncio.run(collect())

    assert len(provider.requests) == 22
    assert result.state is AgentRunState.SETTLED
    assert result.message is not None
    assert result.message.text == "complete"
    assert events[-1].kind is AgentSessionEventKind.RUN_SETTLED


def test_run_state_space_names_all_required_states() -> None:
    assert {state.value for state in AgentRunState} == {
        "active",
        "settled",
        "cancelled",
        "failed",
    }
    assert AgentRunState.ACTIVE.terminal is False
    assert all(state.terminal for state in AgentRunState if state is not AgentRunState.ACTIVE)


def test_agent_run_persists_only_the_authoritative_message_end() -> None:
    store = InMemorySessionStore()
    ids = iter(("configuration", "user-message", "authoritative-message"))
    kernel = AgentKernel.with_new_session(
        FakeProvider.streamed_run(),
        store,
        session_id="session-run",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    kernel.drain_session_events()

    async def collect() -> list[AgentSessionEvent]:
        run = kernel.create_run("test input")
        return [event async for event in run]

    events = asyncio.run(collect())
    kinds = _kinds(events)
    message_end_index = kinds.index(AgentSessionEventKind.MESSAGE_END)

    assert kinds[:2] == [
        AgentSessionEventKind.SESSION_ENTRY,
        AgentSessionEventKind.ACTIVE_BRANCH,
    ]
    assert kinds[message_end_index + 1 : message_end_index + 3] == [
        AgentSessionEventKind.SESSION_ENTRY,
        AgentSessionEventKind.ACTIVE_BRANCH,
    ]
    messages = [entry for entry in kernel.session_active_branch if entry.kind == "message"]
    assert [(entry.payload["role"], entry.payload["text"]) for entry in messages] == [
        ("user", "test input"),
        ("assistant", "Hello from the Fake Provider."),
    ]
    assert messages[1].entry_id == "authoritative-message"
    persisted_json = messages[1].payload_json
    assert "message_update" not in persisted_json
    assert "thinking_delta" not in persisted_json
    assert "tool_progress" not in persisted_json
