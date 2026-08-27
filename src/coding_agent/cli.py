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

from coding_agent.context import ContextInput, ContextPipeline, ContextSettings
from coding_agent.control import RetryPolicy
from coding_agent.environment import LocalCodingEnvironment
from coding_agent.events import (
    AgentError,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    ProviderDone,
    ProviderError,
    ProviderStreamEvent,
    ProviderTextDelta,
    ToolResult,
    assistant_message_record,
)
from coding_agent.kernel import AgentKernel
from coding_agent.provider import FakeProvider, ModelMessage, UserMessage
from coding_agent.session import JsonlSessionStore, Session, SessionError
from coding_agent.tool_runtime import ToolRuntime


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
    record: dict[str, Any] = {"event": event.kind.value}
    if event.run_id is not None:
        record["run_id"] = event.run_id
    if event.session_id is not None:
        record["session_id"] = event.session_id
    if event.session_entry is not None:
        record["session_entry"] = {
            "entry_id": event.session_entry.entry_id,
            "parent_id": event.session_entry.parent_id,
            "kind": event.session_entry.kind,
            "payload": event.session_entry.payload,
        }
    if event.active_branch is not None:
        record["active_branch"] = list(event.active_branch)
    if event.context_stage is not None:
        record["context_stage"] = event.context_stage
    if event.configuration_json is not None:
        record["configuration"] = json.loads(event.configuration_json)
    if event.agent_event is not None:
        if event.agent_event.turn_id is not None:
            record["turn_id"] = event.agent_event.turn_id
        if event.agent_event.message_id is not None:
            record["message_id"] = event.agent_event.message_id
    if event.message is not None:
        record["message"] = assistant_message_record(event.message)
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
    if event.pending_message is not None:
        record["pending_message"] = {
            "message_id": event.pending_message.message_id,
            "kind": event.pending_message.kind.value,
            "text": event.pending_message.text,
        }
        record["queue_size"] = event.queue_size
    if event.retry_error is not None:
        record["attempt"] = event.retry_attempt
        record["remaining"] = event.retry_remaining
        record["error"] = _error_record(event.retry_error)
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
        result_record["message"] = assistant_message_record(result.message)
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


def _run_control_script(case: str) -> tuple[tuple[ProviderStreamEvent, ...], ...]:
    if case == "steering":
        return (
            (
                *_scripted_tool_call_events(0, "read-control", "read", {"path": "state.txt"}),
                ProviderDone("tool_use"),
            ),
            (ProviderTextDelta("Steering applied after the tool result."), ProviderDone()),
        )
    if case == "follow-up":
        return (
            (ProviderTextDelta("Initial work complete."), ProviderDone()),
            (ProviderTextDelta("Follow-up work complete."), ProviderDone()),
        )
    if case == "cancel":
        return (
            (
                ProviderTextDelta("Provider work is in flight."),
                ProviderTextDelta("This delta must not be observed."),
                ProviderDone(),
            ),
        )
    if case == "retry-success":
        return (
            (
                ProviderTextDelta("Discarded partial attempt."),
                ProviderError("provider_unavailable", "Temporary scripted failure."),
            ),
            (ProviderTextDelta("Retry recovered."), ProviderDone()),
        )
    return ((ProviderError("provider_unavailable", "Scripted retry exhaustion."),),)


async def _run_control_demo(case: str) -> int:
    with tempfile.TemporaryDirectory(prefix="coding-agent-run-control-") as directory:
        workspace = Path(directory)
        (workspace / "state.txt").write_text("ready\n", encoding="utf-8")
        provider = FakeProvider(_run_control_script(case))
        store = JsonlSessionStore(workspace / "session.jsonl")
        runtime = ToolRuntime(LocalCodingEnvironment(workspace))
        kernel = AgentKernel.with_new_session(
            provider,
            store,
            configuration={"provider": "fake", "demo": "run-control"},
            session_id="run-control-demo",
            tool_runtime=runtime,
            retry_policy=RetryPolicy(max_attempts=2 if case == "retry-success" else 3),
        )
        run = kernel.create_run("Demonstrate deterministic Agent Run control.")
        controlled = False
        if case == "cancel":
            await run.steer("queued steering")
            await run.follow_up("queued follow-up")

        events: list[AgentSessionEvent] = []
        async for event in run:
            events.append(event)
            _print_record(_event_record(event))
            if (
                case == "steering"
                and not controlled
                and event.kind is AgentSessionEventKind.TOOL_EXECUTION_START
            ):
                await run.steer("inspect the tool result")
                controlled = True
            elif (
                case == "follow-up"
                and not controlled
                and event.kind is AgentSessionEventKind.MESSAGE_UPDATE
            ):
                await run.follow_up("continue with follow-up work")
                controlled = True
            elif (
                case == "cancel"
                and not controlled
                and event.kind is AgentSessionEventKind.MESSAGE_UPDATE
            ):
                await run.cancel()
                controlled = True

        result = await run.result()
        _print_record(_result_record(result))
        terminal_kinds = {
            AgentSessionEventKind.RUN_SETTLED,
            AgentSessionEventKind.RUN_CANCELLED,
            AgentSessionEventKind.RUN_FAILED,
        }
        requests = provider.requests
        session_user_messages = [
            entry.payload.get("text")
            for entry in kernel.session_active_branch
            if entry.kind == "message" and entry.payload.get("role") == "user"
        ]
        _print_record(
            {
                "run_control": {
                    "case": case,
                    "provider_request_count": len(requests),
                    "provider_request_messages": [
                        [
                            {
                                "role": message.role,
                                "text": getattr(message, "text", None),
                            }
                            for message in request.messages
                        ]
                        for request in requests
                    ],
                    "same_request_retried": len(requests) > 1
                    and all(request is requests[0] for request in requests[1:]),
                    "session_user_messages": session_user_messages,
                    "injected_messages": [
                        event.pending_message.text
                        for event in events
                        if event.kind is AgentSessionEventKind.MESSAGE_INJECTED
                        and event.pending_message is not None
                    ],
                    "terminal_count": sum(event.kind in terminal_kinds for event in events),
                    "result_state": result.state.value,
                }
            }
        )
        expected = {
            "steering": AgentRunState.SETTLED,
            "follow-up": AgentRunState.SETTLED,
            "cancel": AgentRunState.CANCELLED,
            "retry-success": AgentRunState.SETTLED,
            "retry-failure": AgentRunState.FAILED,
        }[case]
        return 0 if result.state is expected else 1


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


async def _run_session_message(kernel: AgentKernel) -> None:
    run = kernel.create_run("Continue the deterministic Session.")
    async for event in run:
        _print_record(_event_record(event))
    _print_record(_result_record(await run.result()))


def _invalid_session_records() -> list[dict[str, object]]:
    common: dict[str, object] = {
        "schema": "coding-agent-session",
        "version": 1,
        "session_id": "session-invalid",
    }
    return [
        {
            **common,
            "sequence": 1,
            "record_type": "entry",
            "entry": {
                "entry_id": "root",
                "parent_id": None,
                "kind": "configuration",
                "payload": {"provider": "fake"},
            },
        },
        {
            **common,
            "sequence": 2,
            "record_type": "entry",
            "entry": {
                "entry_id": "orphan",
                "parent_id": "missing-parent",
                "kind": "message",
                "payload": {"role": "assistant", "text": "invalid"},
            },
        },
        {**common, "sequence": 3, "record_type": "closed"},
    ]


async def _session_tree_demo(case: str) -> int:
    workspace = Path(tempfile.mkdtemp(prefix="coding-agent-session-tree-"))
    jsonl_path = workspace / "session.jsonl"
    if case == "invalid-entry":
        jsonl_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for record in _invalid_session_records()
            ),
            encoding="utf-8",
        )
        try:
            AgentKernel.with_resumed_session(
                FakeProvider(()), JsonlSessionStore(jsonl_path), "session-invalid"
            )
        except SessionError as exc:
            _print_record(
                {
                    "session_rejected": {
                        "code": exc.code,
                        "message": str(exc),
                        "jsonl_path": str(jsonl_path),
                    }
                }
            )
            return 1
        raise AssertionError("invalid Session history was accepted")

    ids = iter(("entry-0001", "entry-0002", "entry-0003"))
    first_provider = FakeProvider(((ProviderTextDelta("original route"), ProviderDone()),))
    kernel = AgentKernel.with_new_session(
        first_provider,
        JsonlSessionStore(jsonl_path),
        session_id="session-demo",
        configuration={"provider": "fake", "schema_version": 1},
        entry_id_factory=lambda: next(ids),
    )
    for event in kernel.drain_session_events():
        _print_record(_event_record(event))
    fork_parent = kernel.session_active_leaf_id
    await _run_session_message(kernel)
    kernel.close_session()

    reloaded_store = JsonlSessionStore(jsonl_path)
    second_provider = FakeProvider(((ProviderTextDelta("alternate route"), ProviderDone()),))
    resumed = AgentKernel.with_resumed_session(
        second_provider,
        reloaded_store,
        "session-demo",
        entry_id_factory=iter(("entry-0004", "entry-0005")).__next__,
    )
    for event in resumed.drain_session_events():
        _print_record(_event_record(event))
    resumed.fork_session(fork_parent)
    for event in resumed.drain_session_events():
        _print_record(_event_record(event))
    await _run_session_message(resumed)
    resumed.close_session()

    _print_record(
        {
            "session_tree": {
                "session_id": resumed.session_id,
                "branches": [
                    [entry.entry_id for entry in branch] for branch in resumed.session_branches
                ],
                "active_branch": [entry.entry_id for entry in resumed.session_active_branch],
                "active_messages": [
                    str(entry.payload["text"])
                    for entry in resumed.session_active_branch
                    if entry.kind == "message"
                ],
            }
        }
    )
    records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    _print_record({"jsonl": {"path": str(jsonl_path), "records": records}})
    return 0


class _DemoFailingSummarizer:
    def summarize(self, messages: tuple[ModelMessage, ...]) -> str:
        raise RuntimeError("deterministic summary failure")


def _context_demo_session(path: Path) -> tuple[Session, tuple[str, ...]]:
    ids = iter(
        (
            "entry-root",
            "entry-sibling-user",
            "entry-sibling-answer",
            "entry-active-user",
            "entry-active-answer",
            "entry-checkpoint",
            "entry-current-user",
            "entry-current-answer",
        )
    )
    session = Session.create(
        JsonlSessionStore(path),
        session_id="session-context-demo",
        configuration={"provider": "fake", "schema_version": 1},
        entry_id_factory=lambda: next(ids),
    )
    root = session.active_leaf_id
    session.record_user_message("SIBLING_MARKER")
    session.record_authoritative_message(AssistantMessage(text="sibling answer"))
    session.fork(root)
    session.record_user_message("ACTIVE_BRANCH_HISTORY " * 90)
    session.record_authoritative_message(AssistantMessage(text="active answer " * 90))
    original_ids = tuple(
        sorted({entry.entry_id for branch in session.branches for entry in branch})
    )
    session.drain_events()
    return session, original_ids


def _context_record(
    context: Any,
    *,
    pending_marker: str | None = None,
    injected_marker: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "estimated_characters": context.estimated_characters,
        "max_characters": context.max_characters,
        "bounded": context.bounded,
        "estimator": "canonical_json_characters",
        "assembly_order": list(context.assembly_order),
        "message_roles": [message.role for message in context.provider_request.messages],
    }
    request_text = repr(context.provider_request)
    if pending_marker is not None:
        record["pending_provided"] = True
        record["request_contains_pending_marker"] = pending_marker in request_text
    if injected_marker is not None:
        record["injected_provided"] = True
        record["request_contains_injected_marker"] = injected_marker in request_text
    return record


async def _context_compaction_demo(case: str) -> int:
    with tempfile.TemporaryDirectory(prefix="coding-agent-context-") as temporary:
        jsonl_path = Path(temporary) / "session.jsonl"
        session, original_ids = _context_demo_session(jsonl_path)
        injected = UserMessage(text="INJECTED_MARKER")
        pending = UserMessage(text="PENDING_MARKER")
        before = (
            ContextPipeline()
            .build(
                ContextInput(
                    settings=ContextSettings(max_characters=100_000),
                    active_branch=session.active_branch,
                    injected_messages=(injected,),
                    pending_messages=(pending,),
                )
            )
            .context
        )
        _print_record(
            {
                "context_before": _context_record(
                    before,
                    pending_marker=pending.text,
                    injected_marker=injected.text,
                )
            }
        )

        pipeline = (
            ContextPipeline(_DemoFailingSummarizer())
            if case == "summary-error"
            else ContextPipeline()
        )
        provider = FakeProvider(((ProviderTextDelta("bounded context accepted"), ProviderDone()),))
        kernel = AgentKernel(
            provider,
            session=session,
            context_pipeline=pipeline,
            context_settings=ContextSettings(
                project_context=("PROJECT_RESOURCE",),
                max_characters=650,
            ),
        )
        run = kernel.create_run(injected.text)
        async for event in run:
            _print_record(_event_record(event))
        result = await run.result()
        _print_record(_result_record(result))

        all_entries = {entry.entry_id for branch in session.branches for entry in branch}
        checkpoint_count = sum(
            entry.kind == "compaction" for branch in session.branches for entry in branch
        )
        originals_preserved = set(original_ids).issubset(all_entries)

        if case == "summary-error":
            session.close()
            resumable = False
            try:
                Session.resume(JsonlSessionStore(jsonl_path), session.session_id)
                resumable = True
            except SessionError:
                pass
            _print_record(
                {
                    "context_failure": {
                        "code": None if result.error is None else result.error.code,
                        "provider_calls": len(provider.requests),
                        "checkpoint_count": checkpoint_count,
                        "original_entries_preserved": originals_preserved,
                        "session_resumable": resumable,
                    }
                }
            )
            return 1

        after = kernel.model_contexts[-1]
        _print_record({"context_after": _context_record(after)})
        request_text = repr(provider.requests[-1])
        _print_record(
            {
                "provider_request": {
                    "provider_calls": len(provider.requests),
                    "contains_sibling_marker": "SIBLING_MARKER" in request_text,
                    "contains_pending_marker": pending.text in request_text,
                    "contains_injected_marker": injected.text in request_text,
                    "message_roles": [message.role for message in provider.requests[-1].messages],
                    "estimated_characters": after.estimated_characters,
                }
            }
        )
        _print_record(
            {
                "session_tree": {
                    "branches": [
                        [entry.entry_id for entry in branch] for branch in session.branches
                    ],
                    "active_branch": [entry.entry_id for entry in session.active_branch],
                    "original_entry_ids": list(original_ids),
                    "original_entries_preserved": originals_preserved,
                    "checkpoint_count": checkpoint_count,
                    "jsonl_path": str(jsonl_path),
                }
            }
        )
        return 0


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
    session_tree = demos.add_parser(
        "session-tree", help="create, reload, resume, and fork an inspectable Session tree"
    )
    session_tree.add_argument(
        "--case",
        choices=("success", "invalid-entry"),
        default="success",
        help="scripted Session persistence outcome",
    )
    context_compaction = demos.add_parser(
        "context-compaction",
        help="project and compact one deterministic Active Branch",
    )
    context_compaction.add_argument(
        "--case",
        choices=("success", "summary-error"),
        default="success",
        help="scripted Context construction outcome",
    )
    run_control = demos.add_parser(
        "run-control", help="observe steering, follow-up, cancellation, and Provider retry"
    )
    run_control.add_argument(
        "--case",
        choices=("steering", "follow-up", "cancel", "retry-success", "retry-failure"),
        default="steering",
        help="deterministic Agent Run control scenario",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse Host arguments, drive AgentRun, and render public events."""

    args = _parser().parse_args(argv)
    if args.command == "demo" and args.demo == "streamed-run":
        return asyncio.run(_streamed_run_demo(args.case))
    if args.command == "demo" and args.demo == "tool-loop":
        return asyncio.run(_tool_loop_demo(args.case))
    if args.command == "demo" and args.demo == "session-tree":
        return asyncio.run(_session_tree_demo(args.case))
    if args.command == "demo" and args.demo == "context-compaction":
        return asyncio.run(_context_compaction_demo(args.case))
    if args.command == "demo" and args.demo == "run-control":
        return asyncio.run(_run_control_demo(args.case))
    raise AssertionError("argparse accepted an unsupported command")
