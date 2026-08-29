"""Thin Terminal CLI Host for the public AgentKernel/AgentRun seam."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import difflib
import getpass
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from coding_agent.context import ContextInput, ContextPipeline, ContextSettings
from coding_agent.control import RetryPolicy
from coding_agent.deepseek import (
    DEEPSEEK_MODELS,
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekConfigurationError,
    DeepSeekProvider,
)
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
    ToolCall,
    ToolResult,
    assistant_message_record,
)
from coding_agent.extensions import (
    ExtensionEvent,
    ExtensionEventKind,
    ExtensionRegistry,
    Hook,
    ToolCallHookInput,
    Transform,
)
from coding_agent.extensions_example import ExtensionDemoCase, run_extension_demo
from coding_agent.kernel import AgentKernel
from coding_agent.permissions import PermissionMode
from coding_agent.provider import FakeProvider, ModelMessage, ToolResultMessage, UserMessage
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
    if event.permission_request is not None:
        request = event.permission_request
        record["permission_request"] = {
            "request_id": request.request_id,
            "call_id": request.call_id,
            "tool_name": request.tool_name,
            "mode": request.mode.value,
            "final_arguments": request.final_arguments,
            "operation_intent": request.intent.record(),
            "reason": request.reason,
        }
    if event.permission_decision is not None:
        decision = event.permission_decision
        record["permission_decision"] = {
            "request_id": event.permission_request_id,
            "call_id": decision.call_id,
            "tool_name": decision.tool_name,
            "mode": decision.mode.value,
            "resolution": decision.resolution,
            "source": decision.source,
            "final_arguments": decision.final_arguments,
            "operation_intent": decision.intent.record(),
            "reason": decision.reason,
        }
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


def _extension_event_record(event: ExtensionEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event": event.kind.value,
        "extension": event.extension_name,
        "hook": None if event.hook is None else event.hook.value,
        "capability": event.capability,
        "outcome": event.outcome,
        "revalidated": event.revalidated,
        "code": event.code,
        "message": event.message,
    }


def _print_record(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def _startup_error(code: str, message: str) -> int:
    _print_record({"startup_error": {"code": code, "message": message}})
    return 2


def _default_session_file() -> Path:
    local_state = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if local_state:
        root = Path(local_state)
    else:
        root = Path.home() / ".local" / "state"
    return root / "coding-agent-kernel" / "sessions.jsonl"


def _workspace_snapshot(root: Path, *, excluded: set[Path]) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        names[:] = [
            name for name in names if name != ".git" and not (directory_path / name).is_symlink()
        ]
        for name in files:
            path = directory_path / name
            if path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved in excluded:
                continue
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = path.read_bytes()
    return snapshot


def _workspace_record(
    root: Path,
    before: dict[str, bytes],
    after: dict[str, bytes],
) -> dict[str, object]:
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    patch_lines: list[str] = []
    binary_paths: list[str] = []
    for path in changed:
        before_bytes = before.get(path, b"")
        after_bytes = after.get(path, b"")
        try:
            before_text = before_bytes.decode("utf-8")
            after_text = after_bytes.decode("utf-8")
        except UnicodeDecodeError:
            binary_paths.append(path)
            continue
        from_path = f"a/{path}" if path in before else "/dev/null"
        to_path = f"b/{path}" if path in after else "/dev/null"
        patch_lines.extend(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=from_path,
                tofile=to_path,
            )
        )
    return {
        "root": str(root),
        "changed_paths": changed,
        "patch": "".join(patch_lines),
        "binary_paths": binary_paths,
    }


async def _read_permission_decision(request_id: str) -> bool:
    _print_record(
        {
            "permission_prompt": {
                "request_id": request_id,
                "choices": ["approve", "deny"],
                "default": "deny",
            }
        }
    )
    answer = (await asyncio.to_thread(sys.stdin.readline)).strip().casefold()
    return answer in {"approve", "approved", "a", "y", "yes"}


async def _drive_agent_run(kernel: AgentKernel, task: str, mode: str) -> AgentRunResult:
    run = kernel.create_run(task, permission_mode=mode)
    async for event in run:
        _print_record(_event_record(event))
        if event.permission_request is not None:
            approved = await _read_permission_decision(event.permission_request.request_id)
            await run.resolve_permission(event.permission_request.request_id, approved)
    return await run.result()


def _deepseek_run(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None) -> int:
    try:
        provider = DeepSeekProvider(
            model=args.model,
            transport=transport,
        )
    except DeepSeekConfigurationError as exc:
        return _startup_error(exc.code, str(exc))

    inherited_api_key = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        return _run_deepseek_session(args, provider)
    finally:
        if inherited_api_key is not None:
            os.environ["DEEPSEEK_API_KEY"] = inherited_api_key


def _run_deepseek_session(args: argparse.Namespace, provider: DeepSeekProvider) -> int:
    try:
        workspace = Path(args.workspace).resolve(strict=True)
    except OSError as exc:
        return _startup_error(
            "workspace_invalid",
            f"Workspace could not be resolved: {type(exc).__name__}: {exc}",
        )
    if not workspace.is_dir():
        return _startup_error("workspace_invalid", "Workspace must be an existing directory.")

    session_file = Path(args.session_file or _default_session_file()).resolve()
    before = _workspace_snapshot(workspace, excluded={session_file})
    try:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        store = JsonlSessionStore(session_file)
        runtime = ToolRuntime(LocalCodingEnvironment(workspace))
        runtime.enable("grep", "find", "ls")
        context_settings = ContextSettings(project_context=(f"Workspace root: {workspace}",))
        if args.resume is None:
            kernel = AgentKernel.with_new_session(
                provider,
                store,
                configuration={
                    "provider": "deepseek",
                    "model": args.model,
                    "workspace": str(workspace),
                },
                session_id=args.session_id,
                tool_runtime=runtime,
                context_settings=context_settings,
            )
            resumed = False
        else:
            kernel = AgentKernel.with_resumed_session(
                provider,
                store,
                args.resume,
                tool_runtime=runtime,
                context_settings=context_settings,
            )
            resumed = True
    except (OSError, SessionError, ValueError) as exc:
        return _startup_error(
            "session_start_failed",
            f"Session could not be started: {type(exc).__name__}: {exc}",
        )

    if args.mode == PermissionMode.FULL.value:
        _print_record(
            {
                "warning": {
                    "code": "full_permission_mode",
                    "message": (
                        "RISK: full bypasses Kernel approval and workspace containment; "
                        "OS authority, cancellation, timeout, and process lifecycle are unchanged."
                    ),
                }
            }
        )

    result = asyncio.run(_drive_agent_run(kernel, args.task, args.mode))
    kernel.close_session()
    after = _workspace_snapshot(workspace, excluded={session_file})
    _print_record(_result_record(result))
    _print_record(
        {
            "session": {
                "session_id": kernel.session_id,
                "path": str(session_file),
                "resumed": resumed,
            }
        }
    )
    _print_record({"workspace": _workspace_record(workspace, before, after)})
    return int(result.state is not AgentRunState.SETTLED)


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
                    and all(request == requests[0] for request in requests[1:]),
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


async def _extensions_demo(case: ExtensionDemoCase) -> int:
    report = await run_extension_demo(case)
    for session_event in report.events:
        _print_record(_event_record(session_event))
    _print_record(_result_record(report.result))
    for extension_event in report.extension_events:
        _print_record({"extension_event": _extension_event_record(extension_event)})

    tool_results = [
        result
        for request in report.provider_requests
        for message in request.messages
        if isinstance(message, ToolResultMessage)
        for result in message.results
    ]
    by_call_id = {result.call_id: result for result in tool_results}
    changes = [
        event
        for event in report.extension_events
        if event.kind is ExtensionEventKind.HANDLER_OUTCOME
        and event.outcome in {"transform", "supplement"}
    ]
    error = report.result.error
    summary: dict[str, Any] = {
        "case": case,
        "terminal_state": report.result.state.value,
        "provider_calls": len(report.provider_requests),
        "context_project_context": (
            []
            if not report.provider_requests
            else list(report.provider_requests[0].project_context)
        ),
        "input": (
            None
            if not report.provider_requests or not report.provider_requests[0].messages
            else getattr(report.provider_requests[0].messages[-1], "text", None)
        ),
        "custom_tool_result": (
            None if "allowed" not in by_call_id else _tool_result_record(by_call_id["allowed"])
        ),
        "blocked_tool_result": (
            None if "blocked" not in by_call_id else _tool_result_record(by_call_id["blocked"])
        ),
        "custom_session_entries": [
            {"kind": entry.kind, "payload": entry.payload}
            for entry in report.session_entries
            if entry.kind not in {"configuration", "message", "compaction", "permission_decision"}
        ],
        "ordered_outcomes": [
            {
                "extension": event.extension_name,
                "hook": None if event.hook is None else event.hook.value,
                "outcome": event.outcome,
            }
            for event in changes
        ],
        "all_changes_revalidated": all(event.revalidated for event in changes),
        "extension_events_separate": not any(
            event.kind.value in {kind.value for kind in ExtensionEventKind}
            for event in report.events
        ),
        "session_unchanged": [entry.kind for entry in report.session_entries] == ["configuration"],
        "handler_failure_observed": any(
            event.kind is ExtensionEventKind.HANDLER_FAILED for event in report.extension_events
        ),
        "error": None if error is None else _error_record(error),
        "jsonl_path": str(report.jsonl_path),
    }
    _print_record({"extensions": summary})

    if case == "success":
        return int(
            report.result.state is not AgentRunState.SETTLED
            or by_call_id.get("allowed") is None
            or by_call_id.get("blocked") is None
            or not summary["custom_session_entries"]
        )
    if case == "ordering":
        return int(
            report.result.state is not AgentRunState.SETTLED
            or summary["input"] != "ordering|ONE|TWO"
            or summary["context_project_context"] != ["ONE", "TWO"]
            or not summary["all_changes_revalidated"]
        )
    return int(
        report.result.state is not AgentRunState.FAILED
        or len(report.provider_requests) != 0
        or not summary["session_unchanged"]
        or not summary["handler_failure_observed"]
    )


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

    verify = 'findstr "value = 2" sample.py' if os.name == "nt" else 'grep -F "value = 2" sample.py'
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


def _permission_script(
    mode: PermissionMode,
    identity_command: str,
) -> tuple[tuple[ProviderStreamEvent, ...], ...]:
    if mode is PermissionMode.ASK:
        calls = (
            *_scripted_tool_call_events(
                0,
                "approved-write",
                "write",
                {"path": "approved.txt", "content": "approved"},
            ),
            *_scripted_tool_call_events(
                1,
                "denied-write",
                "write",
                {"path": "denied.txt", "content": "denied"},
            ),
        )
    elif mode is PermissionMode.AUTO:
        calls = (
            *_scripted_tool_call_events(
                0,
                "workspace-write",
                "write",
                {"path": "auto.txt", "content": "auto"},
            ),
            *_scripted_tool_call_events(
                1,
                "outside-write",
                "write",
                {"path": "../outside-auto.txt", "content": "blocked"},
            ),
            *_scripted_tool_call_events(
                2,
                "network-shell",
                "bash",
                {"command": "curl https://example.invalid"},
            ),
            *_scripted_tool_call_events(
                3,
                "unknown-shell",
                "bash",
                {"command": "echo changed > auto-shell.txt"},
            ),
        )
    elif mode is PermissionMode.FULL:
        calls = (
            *_scripted_tool_call_events(
                0,
                "outside-write",
                "write",
                {"path": "../outside-full.txt", "content": "full"},
            ),
            *_scripted_tool_call_events(
                1,
                "os-identity",
                "bash",
                {"command": identity_command},
            ),
        )
    else:
        calls = (
            *_scripted_tool_call_events(
                0,
                "workspace-read",
                "read",
                {"path": "input.txt"},
            ),
            *_scripted_tool_call_events(
                1,
                "workspace-write",
                "write",
                {"path": "plan.txt", "content": "blocked"},
            ),
            *_scripted_tool_call_events(
                2,
                "diagnostic-shell",
                "bash",
                {"command": "git status"},
            ),
            *_scripted_tool_call_events(
                3,
                "network-shell",
                "bash",
                {"command": "curl https://example.invalid"},
            ),
            *_scripted_tool_call_events(
                4,
                "outside-read",
                "read",
                {"path": "../outside-source.txt"},
            ),
            *_scripted_tool_call_events(
                5,
                "unknown-shell",
                "bash",
                {"command": "echo changed > plan-shell.txt"},
            ),
        )
    return (
        (*calls, ProviderDone("tool_use")),
        (ProviderTextDelta(f"Permission Mode {mode.value} demo complete."), ProviderDone()),
    )


class _PermissionRewriteExtension:
    name = "permission-rewrite"

    def __init__(self) -> None:
        self._call_count = 0

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._rewrite)

    def _rewrite(self, hook_input: ToolCallHookInput) -> Transform[ToolCall]:
        self._call_count += 1
        arguments = hook_input.arguments
        if self._call_count > 1:
            arguments["path"] = "after.txt"
        return Transform(ToolCall(hook_input.call_id, hook_input.tool_name, arguments))


async def _permission_extension_rewrite_demo(root: Path) -> int:
    workspace = root / "workspace"
    workspace.mkdir()
    provider = FakeProvider(
        (
            (
                *_scripted_tool_call_events(
                    0,
                    "rewrite-write",
                    "write",
                    {"path": "before.txt", "content": "must-not-write"},
                ),
                ProviderDone("tool_use"),
            ),
            (
                *_scripted_tool_call_events(
                    0,
                    "rewrite-write",
                    "write",
                    {"path": "before.txt", "content": "must-not-write"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel.with_new_session(
        provider,
        JsonlSessionStore(root / "session.jsonl"),
        configuration={"provider": "fake", "demo": "permission-rewrite"},
        session_id="permission-rewrite-demo",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
        extensions=(_PermissionRewriteExtension(),),
    )
    run = kernel.create_run("Demonstrate post-Hook binding.", permission_mode=PermissionMode.ASK)
    prior_request_id: str | None = None
    stale_approval_rejected = False
    final_path = ""
    async for event in run:
        record = _event_record(event)
        record["permission_mode"] = PermissionMode.ASK.value
        _print_record(record)
        request = event.permission_request
        if request is None:
            continue
        final_path = str(request.final_arguments["path"])
        if prior_request_id is None:
            prior_request_id = request.request_id
            await run.resolve_permission(request.request_id, True)
            continue
        try:
            await run.resolve_permission(prior_request_id, True)
        except RuntimeError:
            stale_approval_rejected = True
        await run.resolve_permission(request.request_id, False)
    result = await run.result()
    kernel.close_session()
    side_effect_free = (workspace / "before.txt").read_text(
        encoding="utf-8"
    ) == "must-not-write" and not (workspace / "after.txt").exists()
    _print_record(
        {
            "permission_demo": {
                "case": "extension-rewrite",
                "mode": PermissionMode.ASK.value,
                "state": result.state.value,
                "stale_approval_rejected": stale_approval_rejected,
                "final_binding_confirmed": final_path == "after.txt",
                "final_path": final_path,
                "denied_side_effect_free": side_effect_free,
            }
        }
    )
    return int(
        result.state is not AgentRunState.SETTLED
        or not stale_approval_rejected
        or final_path != "after.txt"
        or not side_effect_free
    )


async def _permission_pending_terminal_demo(root: Path, case: str) -> int:
    workspace = root / "workspace"
    workspace.mkdir()
    provider = FakeProvider(
        (
            (
                *_scripted_tool_call_events(
                    0,
                    "pending-write",
                    "write",
                    {"path": "pending.txt", "content": "must-not-write"},
                ),
                ProviderDone("tool_use"),
            ),
        )
    )
    kernel = AgentKernel.with_new_session(
        provider,
        JsonlSessionStore(root / "session.jsonl"),
        configuration={"provider": "fake", "demo": case},
        session_id=f"permission-{case}-demo",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
    )
    run = kernel.create_run("Wait for a Host decision.", permission_mode=PermissionMode.ASK)
    async for event in run:
        record = _event_record(event)
        record["permission_mode"] = PermissionMode.ASK.value
        _print_record(record)
        if event.permission_request is not None:
            if case == "host-disconnect":
                await run.aclose()
            else:
                await run.cancel()
    result = await run.result()
    decisions = [
        entry for entry in kernel.session_active_branch if entry.kind == "permission_decision"
    ]
    denial_persisted = len(decisions) == 1 and decisions[0].payload.get("resolution") == "denied"
    pending_persisted = any(
        "request_id" in entry.payload or "binding" in entry.payload for entry in decisions
    )
    kernel.close_session()
    tool_executed = (workspace / "pending.txt").exists()
    _print_record(
        {
            "permission_demo": {
                "case": case,
                "mode": PermissionMode.ASK.value,
                "state": result.state.value,
                "denial_persisted": denial_persisted,
                "pending_persisted": pending_persisted,
                "tool_executed": tool_executed,
            }
        }
    )
    return int(
        result.state is not AgentRunState.CANCELLED
        or not denial_persisted
        or pending_persisted
        or tool_executed
    )


async def _permission_resume_demo(root: Path) -> int:
    workspace = root / "workspace"
    workspace.mkdir()
    store = JsonlSessionStore(root / "session.jsonl")
    provider = FakeProvider(
        (
            (
                *_scripted_tool_call_events(
                    0,
                    "pending-write",
                    "write",
                    {"path": "pending.txt", "content": "must-not-write"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        configuration={"provider": "fake", "demo": "permission-resume"},
        session_id="permission-resume-demo",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
    )
    pending_run = kernel.create_run(
        "Create a transient request.", permission_mode=PermissionMode.ASK
    )
    async for event in pending_run:
        record = _event_record(event)
        record["permission_mode"] = PermissionMode.ASK.value
        _print_record(record)
        if event.permission_request is not None:
            await pending_run.cancel()
    await pending_run.result()

    full_run = kernel.create_run("Select full explicitly.", permission_mode=PermissionMode.FULL)
    async for event in full_run:
        record = _event_record(event)
        record["permission_mode"] = PermissionMode.FULL.value
        _print_record(record)
    await full_run.result()
    selected_before_resume = full_run.permission_mode
    decisions = [
        entry for entry in kernel.session_active_branch if entry.kind == "permission_decision"
    ]
    denial_persisted = len(decisions) == 1 and decisions[0].payload.get("resolution") == "denied"
    pending_persisted = any(
        "request_id" in entry.payload or "binding" in entry.payload for entry in decisions
    )
    kernel.close_session()

    resumed = AgentKernel.with_resumed_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        "permission-resume-demo",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
    )
    resumed_run = resumed.create_run("Resume with the safe default.")
    async for event in resumed_run:
        record = _event_record(event)
        record["permission_mode"] = resumed_run.permission_mode.value
        _print_record(record)
    result = await resumed_run.result()
    resumed.close_session()
    _print_record(
        {
            "permission_demo": {
                "case": "resume",
                "state": result.state.value,
                "selected_before_resume": selected_before_resume.value,
                "resumed_mode": resumed_run.permission_mode.value,
                "denial_persisted": denial_persisted,
                "pending_persisted": pending_persisted,
            }
        }
    )
    return int(
        result.state is not AgentRunState.SETTLED
        or selected_before_resume is not PermissionMode.FULL
        or resumed_run.permission_mode is not PermissionMode.AUTO
        or not denial_persisted
        or pending_persisted
    )


async def _permissions_demo(mode_value: str, case: str) -> int:
    mode = PermissionMode(mode_value)
    with tempfile.TemporaryDirectory(prefix="coding-agent-permissions-") as temporary:
        root = Path(temporary)
        if case == "extension-rewrite":
            return await _permission_extension_rewrite_demo(root)
        if case in {"cancel", "host-disconnect"}:
            return await _permission_pending_terminal_demo(root, case)
        if case == "resume":
            return await _permission_resume_demo(root)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "input.txt").write_text("input", encoding="utf-8")
        (root / "outside-source.txt").write_text("outside", encoding="utf-8")
        host_identity = getpass.getuser().strip()
        identity_command = subprocess.list2cmdline(
            [sys.executable, "-c", "import getpass; print(getpass.getuser())"]
        )
        provider = FakeProvider(_permission_script(mode, identity_command))
        kernel = AgentKernel.with_new_session(
            provider,
            JsonlSessionStore(root / "session.jsonl"),
            configuration={"provider": "fake", "demo": "permissions"},
            session_id="permission-demo",
            tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
        )
        run = kernel.create_run("Demonstrate Host-controlled permissions.", permission_mode=mode)
        observed_results: dict[str, dict[str, Any]] = {}
        tool_identity: str | None = None
        async for event in run:
            record = _event_record(event)
            record["permission_mode"] = mode.value
            _print_record(record)
            if event.permission_request is not None:
                await run.resolve_permission(
                    event.permission_request.request_id,
                    event.permission_request.call_id == "approved-write",
                )
            if event.tool_result is not None:
                result_record = _tool_result_record(event.tool_result)
                if event.tool_result.call_id == "os-identity" and event.tool_result.output:
                    tool_identity = str(event.tool_result.output.get("stdout", "")).strip()
                observed_results[event.tool_result.call_id] = {
                    "status": result_record["status"],
                    "error": (
                        None if result_record["error"] is None else result_record["error"]["code"]
                    ),
                }
        result = await run.result()
        kernel.close_session()
        os_authority_unchanged = (
            tool_identity is not None and tool_identity.casefold() == host_identity.casefold()
        )
        warning = (
            "RISK: full bypasses Kernel approval and workspace containment; "
            "OS authority, cancellation, timeout, and process lifecycle are unchanged."
            if mode is PermissionMode.FULL
            else ""
        )
        _print_record(
            {
                "permission_demo": {
                    "case": case,
                    "mode": mode.value,
                    "state": result.state.value,
                    "results": observed_results,
                    "approved_exists": (workspace / "approved.txt").exists(),
                    "denied_exists": (workspace / "denied.txt").exists(),
                    "workspace_write_exists": (workspace / "auto.txt").exists(),
                    "outside_exists": (root / f"outside-{mode.value}.txt").exists(),
                    "warning": warning,
                    "os_authority_unchanged": os_authority_unchanged,
                }
            }
        )
        return int(
            result.state is not AgentRunState.SETTLED
            or (mode is PermissionMode.FULL and not os_authority_unchanged)
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
        verification_succeeded = case != "success"
        async for event in run:
            _print_record(_event_record(event))
            if event.tool_result is not None and event.tool_result.call_id == "verify":
                stdout = (
                    "" if event.tool_result.output is None else event.tool_result.output["stdout"]
                )
                verification_succeeded = (
                    event.tool_result.status == "success" and "value = 2" in str(stdout)
                )
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
        return int(result.state is not AgentRunState.SETTLED or not verification_succeeded)


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
    run = commands.add_parser("run", help="run a DeepSeek-backed coding task")
    run.add_argument("task", help="coding task for the Agent Run")
    run.add_argument(
        "--provider",
        choices=("deepseek",),
        required=True,
        help="ModelProvider Adapter",
    )
    run.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="existing disposable or explicitly authorized workspace",
    )
    run.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in PermissionMode),
        default=PermissionMode.AUTO.value,
        help="Host-selected run-scoped Permission Mode",
    )
    run.add_argument(
        "--model",
        choices=DEEPSEEK_MODELS,
        default=DEFAULT_DEEPSEEK_MODEL,
        help="official DeepSeek model identifier",
    )
    run.add_argument(
        "--session-file",
        type=Path,
        help="append-only JSONL Session store (defaults to the user state directory)",
    )
    session_action = run.add_mutually_exclusive_group()
    session_action.add_argument("--session-id", help="explicit ID for a new Session")
    session_action.add_argument("--resume", metavar="SESSION_ID", help="resume a closed Session")
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
    extensions = demos.add_parser(
        "extensions",
        help="run explicit Extension registration, ordering, and rejection scenarios",
    )
    extensions.add_argument(
        "--case",
        choices=("success", "ordering", "invalid-mutation"),
        default="success",
        help="deterministic Extension scenario",
    )
    permissions = demos.add_parser(
        "permissions",
        help="run Host-controlled Permission Mode scenarios",
    )
    permissions.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in PermissionMode),
        default=PermissionMode.AUTO.value,
        help="Host-selected run-scoped Permission Mode",
    )
    permissions.add_argument(
        "--case",
        choices=("standard", "extension-rewrite", "cancel", "host-disconnect", "resume"),
        default="standard",
        help="deterministic permission lifecycle scenario",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    deepseek_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Parse Host arguments, drive AgentRun, and render public events."""

    args = _parser().parse_args(argv)
    if args.command == "run":
        return _deepseek_run(args, deepseek_transport)
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
    if args.command == "demo" and args.demo == "extensions":
        return asyncio.run(_extensions_demo(args.case))
    if args.command == "demo" and args.demo == "permissions":
        return asyncio.run(_permissions_demo(args.mode, args.case))
    raise AssertionError("argparse accepted an unsupported command")
