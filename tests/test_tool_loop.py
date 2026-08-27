from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coding_agent import (
    AgentKernel,
    AgentRunResult,
    AgentSessionEvent,
    AgentSessionEventKind,
    FakeProvider,
    LocalCodingEnvironment,
    ProviderDone,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ToolResultMessage,
    ToolRuntime,
)


def _scripted_tool_call_events(
    index: int, call_id: str, name: str, arguments: str
) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCallStart(index=index),
        ProviderToolCallDelta(
            index=index,
            call_id_delta=call_id,
            tool_name_delta=name,
            arguments_delta=arguments,
        ),
        ProviderToolCallEnd(index=index),
    )


def test_agent_run_executes_tool_batch_and_sends_ordered_results_to_next_turn(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    first_turn = (
        *_scripted_tool_call_events(0, "read-1", "read", '{"path":"sample.py"}'),
        *_scripted_tool_call_events(
            1,
            "edit-1",
            "edit",
            '{"path":"sample.py","old":"value = 1","new":"value = 2"}',
        ),
        *_scripted_tool_call_events(
            2,
            "bash-1",
            "bash",
            json.dumps(
                {
                    "command": 'python -c "from pathlib import Path; '
                    "assert Path('sample.py').read_text().strip() == 'value = 2'; "
                    "print('verified')\""
                }
            ),
        ),
        ProviderDone(stop_reason="tool_use"),
    )
    second_turn: tuple[ProviderStreamEvent, ...] = (
        ProviderTextDelta("Updated sample.py and verified value = 2."),
        ProviderDone(stop_reason="stop"),
    )
    provider = FakeProvider([first_turn, second_turn])
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = AgentKernel(provider, tool_runtime=runtime).create_run("Update the value.")
        events = [event async for event in run]
        return events, await run.result()

    events, result = asyncio.run(collect())

    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert result.message is not None
    assert result.message.text == "Updated sample.py and verified value = 2."
    assert sum(event.kind is AgentSessionEventKind.TURN_START for event in events) == 2
    assert [
        event.tool_result.call_id
        for event in events
        if event.kind is AgentSessionEventKind.TOOL_EXECUTION_END and event.tool_result is not None
    ] == ["read-1", "edit-1", "bash-1"]
    assert len(provider.requests) == 2
    tool_results = provider.requests[1].messages[-1]
    assert isinstance(tool_results, ToolResultMessage)
    assert tuple(result.call_id for result in tool_results.results) == (
        "read-1",
        "edit-1",
        "bash-1",
    )
    assert all(result.status == "success" for result in tool_results.results)
    updates = [
        event for event in events if event.kind is AgentSessionEventKind.TOOL_EXECUTION_UPDATE
    ]
    assert updates
    assert updates[0].tool_progress is not None
    assert updates[0].tool_progress.stream == "stdout"
    assert updates[0].tool_progress.data.strip() == "verified"


def test_agent_can_observe_tool_errors_and_finish_on_the_next_turn(tmp_path: Path) -> None:
    first_turn = (
        *_scripted_tool_call_events(0, "bad-1", "missing", "{}"),
        *_scripted_tool_call_events(1, "bad-2", "read", "{}"),
        ProviderDone(stop_reason="tool_use"),
    )
    second_turn: tuple[ProviderStreamEvent, ...] = (
        ProviderTextDelta("The requested tools failed; no workspace change was made."),
        ProviderDone(),
    )
    provider = FakeProvider([first_turn, second_turn])

    async def collect() -> AgentRunResult:
        run = AgentKernel(
            provider,
            tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        ).create_run("Try invalid calls.")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())
    tool_results = provider.requests[1].messages[-1]

    assert result.message is not None
    assert "failed" in result.message.text
    assert isinstance(tool_results, ToolResultMessage)
    assert [result.error.code if result.error else None for result in tool_results.results] == [
        "unknown_tool",
        "invalid_arguments",
    ]


def test_host_can_cancel_an_active_tool_execution(tmp_path: Path) -> None:
    first_turn = (
        *_scripted_tool_call_events(
            0,
            "slow",
            "bash",
            json.dumps({"command": 'python -c "import time; time.sleep(10)"'}),
        ),
        ProviderDone(stop_reason="tool_use"),
    )

    async def cancel_during_execution() -> tuple[AgentRunResult, list[AgentSessionEvent]]:
        run = AgentKernel(
            FakeProvider([first_turn]),
            tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        ).create_run("Start then cancel.")
        events: list[AgentSessionEvent] = []
        async for event in run:
            events.append(event)
            if event.kind is AgentSessionEventKind.TOOL_EXECUTION_START:
                await run.cancel()
        return await run.result(), events

    result, events = asyncio.run(cancel_during_execution())
    assert result.state.value == "cancelled"
    assert events[-1].kind is AgentSessionEventKind.RUN_CANCELLED


def test_bash_progress_is_observable_before_process_completion(tmp_path: Path) -> None:
    command = (
        "python -c \"import pathlib,time; print('first', flush=True); time.sleep(0.2); "
        "pathlib.Path('done.flag').write_text('done')\""
    )
    first_turn = (
        *_scripted_tool_call_events(0, "progress", "bash", json.dumps({"command": command})),
        ProviderDone(stop_reason="tool_use"),
    )
    second_turn: tuple[ProviderStreamEvent, ...] = (ProviderTextDelta("done"), ProviderDone())

    async def observe() -> AgentRunResult:
        run = AgentKernel(
            FakeProvider([first_turn, second_turn]),
            tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        ).create_run("Observe progress.")
        saw_live_progress = False
        async for event in run:
            if event.kind is AgentSessionEventKind.TOOL_EXECUTION_UPDATE:
                assert not (tmp_path / "done.flag").exists()
                saw_live_progress = True
        assert saw_live_progress
        return await run.result()

    result = asyncio.run(observe())
    assert result.state.value == "settled"
    assert (tmp_path / "done.flag").read_text(encoding="utf-8") == "done"
