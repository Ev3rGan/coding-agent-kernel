"""Thin Terminal CLI Host for the public AgentKernel/AgentRun seam."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any

from coding_agent.events import (
    AgentError,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AssistantMessage,
    ProviderDone,
    ProviderError,
    ProviderTextDelta,
    ProviderThinkingDelta,
)
from coding_agent.kernel import AgentKernel
from coding_agent.provider import FakeProvider


def _message_record(message: AssistantMessage) -> dict[str, str]:
    return {
        "role": message.role,
        "thinking": message.thinking,
        "text": message.text,
    }


def _provider_record(event: object) -> dict[str, str]:
    if isinstance(event, (ProviderTextDelta, ProviderThinkingDelta)):
        return {"type": event.kind.value, "delta": event.delta}
    if isinstance(event, ProviderDone):
        return {"type": event.kind.value, "stop_reason": event.stop_reason}
    if isinstance(event, ProviderError):
        return {"type": event.kind.value, "code": event.code, "message": event.message}
    raise TypeError(f"unsupported provider event: {type(event).__name__}")


def _error_record(error: AgentError) -> dict[str, str]:
    return {"source": error.source, "code": error.code, "message": error.message}


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse Host arguments, drive AgentRun, and render public events."""

    args = _parser().parse_args(argv)
    if args.command == "demo" and args.demo == "streamed-run":
        return asyncio.run(_streamed_run_demo(args.case))
    raise AssertionError("argparse accepted an unsupported command")
