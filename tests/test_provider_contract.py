from __future__ import annotations

import asyncio

from coding_agent import (
    AgentKernel,
    AgentRunResult,
    AgentSessionEvent,
    AgentSessionEventKind,
    FakeProvider,
    ProviderAbort,
    ProviderCancelled,
    ProviderContentEnd,
    ProviderContentStart,
    ProviderDone,
    ProviderStreamEnd,
    ProviderStreamEvent,
    ProviderStreamStart,
    ProviderTextDelta,
    ProviderTextEnd,
    ProviderTextStart,
    ProviderThinkingDelta,
    ProviderThinkingEnd,
    ProviderThinkingStart,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ProviderUsage,
)


def test_complete_provider_event_family_builds_authoritative_message() -> None:
    script: tuple[ProviderStreamEvent, ...] = (
        ProviderStreamStart(model="fake-v1"),
        ProviderContentStart(),
        ProviderThinkingStart(),
        ProviderThinkingDelta("inspect "),
        ProviderThinkingDelta("workspace"),
        ProviderThinkingEnd(),
        ProviderToolCallStart(index=0, call_id="call-", tool_name="re"),
        ProviderToolCallDelta(
            index=0, call_id_delta="1", tool_name_delta="ad", arguments_delta='{"pa'
        ),
        ProviderToolCallDelta(index=0, arguments_delta='th":"README.md"}'),
        ProviderToolCallEnd(index=0),
        ProviderTextStart(),
        ProviderTextDelta("I will inspect the file."),
        ProviderTextEnd(),
        ProviderContentEnd(),
        ProviderUsage(input_tokens=12, output_tokens=8),
        ProviderDone(stop_reason="tool_use", response_id="response-1"),
        ProviderStreamEnd(),
    )
    provider = FakeProvider([script])

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = AgentKernel(provider).create_run("inspect")
        events = [event async for event in run]
        return events, await run.result()

    events, result = asyncio.run(collect())
    updates = [event for event in events if event.kind is AgentSessionEventKind.MESSAGE_UPDATE]
    message_ends = [event for event in events if event.kind is AgentSessionEventKind.MESSAGE_END]

    assert len(updates) == 17
    assert all(event.message is not None and event.provider_event is not None for event in updates)
    assert len(message_ends) == 1
    message = message_ends[0].message
    assert message is not None
    assert message.text == "I will inspect the file."
    assert message.thinking == "inspect workspace"
    assert message.stop_reason == "tool_use"
    assert message.response_id == "response-1"
    assert message.usage is not None
    assert (message.usage.input_tokens, message.usage.output_tokens) == (12, 8)
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0].call_id == "call-1"
    assert message.tool_calls[0].tool_name == "read"
    assert message.tool_calls[0].arguments == {"path": "README.md"}
    assert result.message == message


def test_provider_abort_is_a_canonical_structured_failure() -> None:
    script: tuple[ProviderStreamEvent, ...] = (
        ProviderStreamStart(),
        ProviderAbort("host_cancelled"),
    )
    provider = FakeProvider([script])

    async def collect() -> AgentRunResult:
        run = AgentKernel(provider).create_run("cancel")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())
    assert result.error is not None
    assert result.error.code == "provider_aborted"
    assert result.error.message == "host_cancelled"


def test_provider_cancelled_settles_the_agent_run_as_cancelled() -> None:
    script: tuple[ProviderStreamEvent, ...] = (
        ProviderStreamStart(),
        ProviderCancelled("request_cancelled"),
    )

    async def collect() -> AgentRunResult:
        run = AgentKernel(FakeProvider([script])).create_run("cancel")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())
    assert result.state.value == "cancelled"
    assert result.error is None


def test_interleaved_tool_call_completion_preserves_provider_index_order() -> None:
    script: tuple[ProviderStreamEvent, ...] = (
        ProviderToolCallStart(index=0),
        ProviderToolCallStart(index=1),
        ProviderToolCallDelta(
            index=1,
            call_id_delta="second",
            tool_name_delta="read",
            arguments_delta='{"path":"b"}',
        ),
        ProviderToolCallEnd(index=1),
        ProviderToolCallDelta(
            index=0,
            call_id_delta="first",
            tool_name_delta="read",
            arguments_delta='{"path":"a"}',
        ),
        ProviderToolCallEnd(index=0),
        ProviderDone(stop_reason="tool_use"),
    )

    async def collect() -> AgentRunResult:
        run = AgentKernel(FakeProvider([script])).create_run("interleaved")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())
    assert result.message is not None
    assert tuple(call.call_id for call in result.message.tool_calls) == ("first", "second")
