"""Thin Terminal CLI Host for the public AgentKernel/AgentRun seam."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import difflib
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from coding_agent.environment import LocalCodingEnvironment
from coding_agent.events import (
    AgentError,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AssistantMessage,
    ProviderDone,
    ProviderStreamEvent,
    ProviderTextDelta,
    ToolResult,
)
from coding_agent.kernel import AgentKernel
from coding_agent.provider import FakeProvider
from coding_agent.tool_runtime import ToolRuntime


def _message_record(message: AssistantMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "thinking": message.thinking,
        "text": message.text,
        "tool_calls": [
            {
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "arguments": call.arguments,
            }
            for call in message.tool_calls
        ],
        "usage": None
        if message.usage is None
        else {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
        "stop_reason": message.stop_reason,
        "response_id": message.response_id,
    }


def _provider_record(event: ProviderStreamEvent) -> dict[str, Any]:
    record: dict[str, Any] = {"type": event.kind.value}
    for item in dataclasses.fields(event):
        if item.name != "kind":
            record[item.name] = getattr(event, item.name)
    return record


def _error_record(error: AgentError) -> dict[str, str]:
    return {"source": error.source, "code": error.code, "message": error.message}


def _tool_result_record(result: ToolResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "tool_name": result.tool_name,
        "status": result.status,
        "output": result.output,
        "error": None
        if result.error is None
        else {"code": result.error.code, "message": result.error.message},
    }


def _event_record(event: AgentSessionEvent) -> dict[str, Any]:
    record: dict[str, Any] = {"event": event.kind.value, "run_id": event.run_id}
    if event.agent_event is not None:
        if event.agent_event.turn_id is not None:
            record["turn_id"] = event.agent_event.turn_id
        if event.agent_event.message_id is not None:
            record["message_id"] = event.agent_event.message_id
    if event.message is not None:
        record["message"] = _message_record(event.message)
    if event.provider_event is not None:
        record["provider_event"] = _provider_record(event.provider_event)
    if event.error is not None:
        record["error"] = _error_record(event.error)
    if event.tool_call is not None:
        record["tool_call"] = {
            "call_id": event.tool_call.call_id,
            "tool_name": event.tool_call.tool_name,
            "arguments": event.tool_call.arguments,
        }
    if event.tool_result is not None:
        record["tool_result"] = _tool_result_record(event.tool_result)
    if event.tool_progress is not None:
        record["tool_progress"] = {
            "call_id": event.tool_progress.call_id,
            "tool_name": event.tool_progress.tool_name,
            "stream": event.tool_progress.stream,
            "data": event.tool_progress.data,
        }
    if event.agent_event is not None and event.agent_event.batch_mode is not None:
        record["batch_mode"] = event.agent_event.batch_mode
    if event.result is not None:
        record["state"] = event.result.state.value
    return record


def _result_record(result: AgentRunResult) -> dict[str, Any]:
    record: dict[str, Any] = {
        "result": {
            "run_id": result.run_id,
            "state": result.state.value,
        }
    }
    result_record = record["result"]
    if result.message is not None:
        result_record["message"] = _message_record(result.message)
    if result.error is not None:
        result_record["error"] = _error_record(result.error)
    return record


def _print_record(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


async def _streamed_run_demo(case: str) -> int:
    provider = (
        FakeProvider.provider_error() if case == "provider-error" else FakeProvider.streamed_run()
    )
    kernel = AgentKernel(provider)
    run = kernel.create_run("Demonstrate one observable headless Agent Run.")

    async for event in run:
        _print_record(_event_record(event))

    result = await run.result()
    _print_record(_result_record(result))
    return 0 if result.state is AgentRunState.SETTLED else 1


def _scripted_tool_call_events(
    index: int, call_id: str, name: str, arguments: dict[str, Any]
) -> tuple[ProviderStreamEvent, ...]:
    from coding_agent.events import (
        ProviderToolCallDelta,
        ProviderToolCallEnd,
        ProviderToolCallStart,
    )

    return (
        ProviderToolCallStart(index),
        ProviderToolCallDelta(
            index,
            call_id_delta=call_id,
            tool_name_delta=name,
            arguments_delta=json.dumps(arguments),
        ),
        ProviderToolCallEnd(index),
    )


def _tool_loop_script(case: str) -> tuple[tuple[ProviderStreamEvent, ...], ...]:
    if case == "mixed-batch":
        return (
            (
                *_scripted_tool_call_events(0, "read-a", "read", {"path": "a.txt"}),
                *_scripted_tool_call_events(1, "read-b", "read", {"path": "b.txt"}),
                ProviderDone("tool_use"),
            ),
            (
                *_scripted_tool_call_events(0, "read-before", "read", {"path": "sample.py"}),
                *_scripted_tool_call_events(
                    1,
                    "write",
                    "write",
                    {"path": "sample.py", "content": "mixed = 2\n"},
                ),
                *_scripted_tool_call_events(2, "read-after", "read", {"path": "sample.py"}),
                ProviderDone("tool_use"),
            ),
            (ProviderTextDelta("Observed parallel and sequential batches."), ProviderDone()),
        )
    if case == "failure":
        return (
            (
                *_scripted_tool_call_events(0, "unknown", "missing", {}),
                *_scripted_tool_call_events(1, "invalid", "read", {}),
                *_scripted_tool_call_events(2, "failed", "bash", {"command": "exit 9"}),
                ProviderDone("tool_use"),
            ),
            (ProviderTextDelta("Observed three structured tool failures."), ProviderDone()),
        )

    verify = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "assert Path('sample.py').read_text().strip() == 'value = 2'; "
        "print('verified')\""
    )
    return (
        (
            *_scripted_tool_call_events(0, "read", "read", {"path": "sample.py"}),
            *_scripted_tool_call_events(
                1,
                "edit",
                "edit",
                {"path": "sample.py", "old": "value = 1", "new": "value = 2"},
            ),
            *_scripted_tool_call_events(2, "verify", "bash", {"command": verify}),
            ProviderDone("tool_use"),
        ),
        (ProviderTextDelta("Updated sample.py and verified value = 2."), ProviderDone()),
    )


async def _tool_loop_demo(case: str) -> int:
    with tempfile.TemporaryDirectory(prefix="coding-agent-tool-loop-") as temporary:
        workspace = Path(temporary)
        before = "mixed = 1\n" if case == "mixed-batch" else "value = 1\n"
        (workspace / "sample.py").write_text(before, encoding="utf-8")
        (workspace / "a.txt").write_text("A\n", encoding="utf-8")
        (workspace / "b.txt").write_text("B\n", encoding="utf-8")
        provider = FakeProvider(_tool_loop_script(case))
        runtime = ToolRuntime(LocalCodingEnvironment(workspace))
        run = AgentKernel(provider, tool_runtime=runtime).create_run(
            "Run the deterministic coding task."
        )
        async for event in run:
            _print_record(_event_record(event))
        result = await run.result()
        _print_record(_result_record(result))
        after = (workspace / "sample.py").read_text(encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="before/sample.py",
                tofile="after/sample.py",
            )
        )
        _print_record({"workspace": {"before": before, "after": after, "diff": diff}})
        return 0 if result.state is AgentRunState.SETTLED else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m coding_agent")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="run deterministic local demonstrations")
    demos = demo.add_subparsers(dest="demo", required=True)
    streamed_run = demos.add_parser("streamed-run", help="observe one Fake Provider Agent Run")
    streamed_run.add_argument(
        "--case",
        choices=("success", "provider-error"),
        default="success",
        help="scripted provider outcome",
    )
    tool_loop = demos.add_parser("tool-loop", help="run a model-tool-model coding loop")
    tool_loop.add_argument(
        "--case",
        choices=("success", "mixed-batch", "failure"),
        default="success",
        help="scripted tool-loop scenario",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse Host arguments, drive AgentRun, and render public events."""

    args = _parser().parse_args(argv)
    if args.command == "demo" and args.demo == "streamed-run":
        return asyncio.run(_streamed_run_demo(args.case))
    if args.command == "demo" and args.demo == "tool-loop":
        return asyncio.run(_tool_loop_demo(args.case))
    raise AssertionError("argparse accepted an unsupported command")
