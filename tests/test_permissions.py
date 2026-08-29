from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

from coding_agent import (
    AgentKernel,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    ExtensionRegistry,
    FakeProvider,
    Hook,
    InMemorySessionStore,
    JsonlSessionStore,
    LocalCodingEnvironment,
    OperationKind,
    PermissionAction,
    PermissionDecision,
    PermissionMode,
    PermissionPolicy,
    ProviderDone,
    ProviderStreamEvent,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    SessionCorruptionError,
    TargetScope,
    ToolCall,
    ToolCallHookInput,
    ToolResultMessage,
    ToolRuntime,
    Transform,
)


def _tool_call_events(
    index: int,
    call_id: str,
    tool_name: str,
    arguments: dict[str, object],
) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCallStart(index),
        ProviderToolCallDelta(index, call_id_delta=call_id, tool_name_delta=tool_name),
        ProviderToolCallDelta(index, arguments_delta=json.dumps(arguments)),
        ProviderToolCallEnd(index),
    )


class _RewritePathExtension:
    name = "rewrite-path"

    def __init__(self, target: str) -> None:
        self._target = target

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._rewrite)

    def _rewrite(self, hook_input: ToolCallHookInput) -> Transform[ToolCall]:
        arguments = hook_input.arguments
        arguments["path"] = self._target
        return Transform(ToolCall(hook_input.call_id, hook_input.tool_name, arguments))


class _ReadOnlyShellEnvironment(LocalCodingEnvironment):
    @property
    def read_only_shell_guaranteed(self) -> bool:
        return True


class _StatefulEnvironment(LocalCodingEnvironment):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.adapter_state = "ready"

    def resolve_path(self, path: str) -> Path:
        if self.adapter_state != "ready":
            raise RuntimeError("environment adapter state was not preserved")
        return super().resolve_path(path)


class _ExecutionRecordingEnvironment(LocalCodingEnvironment):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.read_paths: list[str] = []

    async def read_text(
        self,
        path: str,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        self.read_paths.append(path)
        return await super().read_text(path, cancel_event)


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory links are not available")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.mark.parametrize(
    ("mode", "call", "expected_action", "expected_scope"),
    (
        (
            PermissionMode.PLAN,
            ToolCall("read", "read", {"path": "inside.txt"}),
            PermissionAction.ALLOW,
            TargetScope.WORKSPACE,
        ),
        (
            PermissionMode.PLAN,
            ToolCall("write", "write", {"path": "inside.txt", "content": "changed"}),
            PermissionAction.DENY,
            TargetScope.WORKSPACE,
        ),
        (
            PermissionMode.PLAN,
            ToolCall("shell", "bash", {"command": "git status"}),
            PermissionAction.DENY,
            TargetScope.UNKNOWN,
        ),
        (
            PermissionMode.ASK,
            ToolCall("write", "write", {"path": "inside.txt", "content": "changed"}),
            PermissionAction.ASK,
            TargetScope.WORKSPACE,
        ),
        (
            PermissionMode.ASK,
            ToolCall("network", "bash", {"command": "curl https://example.invalid"}),
            PermissionAction.ASK,
            TargetScope.WORKSPACE,
        ),
        (
            PermissionMode.AUTO,
            ToolCall("write", "write", {"path": "inside.txt", "content": "changed"}),
            PermissionAction.ALLOW,
            TargetScope.WORKSPACE,
        ),
        (
            PermissionMode.AUTO,
            ToolCall("network", "bash", {"command": "curl https://example.invalid"}),
            PermissionAction.ASK,
            TargetScope.WORKSPACE,
        ),
        (
            PermissionMode.AUTO,
            ToolCall("unknown", "bash", {"command": "echo changed > inside.txt"}),
            PermissionAction.ASK,
            TargetScope.UNKNOWN,
        ),
        (
            PermissionMode.AUTO,
            ToolCall("outside", "read", {"path": "../outside.txt"}),
            PermissionAction.ASK,
            TargetScope.OUTSIDE,
        ),
        (
            PermissionMode.ASK,
            ToolCall("outside", "read", {"path": "../outside.txt"}),
            PermissionAction.ASK,
            TargetScope.OUTSIDE,
        ),
        (
            PermissionMode.AUTO,
            ToolCall("custom", "extension_echo", {"value": "unclassified"}),
            PermissionAction.ASK,
            TargetScope.UNKNOWN,
        ),
        (
            PermissionMode.FULL,
            ToolCall("outside", "read", {"path": "../outside.txt"}),
            PermissionAction.ALLOW,
            TargetScope.OUTSIDE,
        ),
    ),
)
def test_canonical_permission_matrix_uses_normalized_final_arguments(
    tmp_path: Path,
    mode: PermissionMode,
    call: ToolCall,
    expected_action: PermissionAction,
    expected_scope: TargetScope,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = PermissionPolicy(workspace)

    evaluation = policy.evaluate(mode, call)

    assert evaluation.action is expected_action
    assert evaluation.intent.scope is expected_scope


@pytest.mark.parametrize(
    "call",
    (
        ToolCall("read-leading-nul", "read", {"path": "\0target.txt"}),
        ToolCall("read-middle-nul", "read", {"path": "target\0name.txt"}),
        ToolCall("read-trailing-nul", "read", {"path": "target.txt\0"}),
        ToolCall(
            "write-middle-nul",
            "write",
            {"path": "target\0name.txt", "content": "must-not-write"},
        ),
        ToolCall(
            "bash-cwd-middle-nul",
            "bash",
            {"command": "echo diagnostic", "cwd": "target\0directory"},
        ),
        ToolCall(
            "bash-target-middle-nul",
            "bash",
            {"command": "cat target\0name.txt"},
        ),
    ),
)
def test_permission_policy_rejects_nul_in_normalized_path_inputs(
    tmp_path: Path,
    call: ToolCall,
) -> None:
    with pytest.raises(ValueError, match="NUL"):
        PermissionPolicy(tmp_path).evaluate(PermissionMode.AUTO, call)


def test_plan_allows_shell_only_when_the_environment_guarantees_read_only(
    tmp_path: Path,
) -> None:
    call = ToolCall("diagnostic", "bash", {"command": "echo diagnostic"})
    ordinary = ToolRuntime(LocalCodingEnvironment(tmp_path)).evaluate_permission(
        call,
        PermissionMode.PLAN,
    )
    constrained = ToolRuntime(_ReadOnlyShellEnvironment(tmp_path)).evaluate_permission(
        call,
        PermissionMode.PLAN,
    )

    assert ordinary.action is PermissionAction.DENY
    assert constrained.action is PermissionAction.ALLOW


def test_permission_binding_changes_when_an_extension_rewrites_final_arguments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = PermissionPolicy(workspace)
    original = ToolCall("same-call", "write", {"path": "before.txt", "content": "value"})
    rewritten = ToolCall("same-call", "write", {"path": "after.txt", "content": "value"})

    original_evaluation = policy.evaluate(PermissionMode.ASK, original)
    rewritten_evaluation = policy.evaluate(PermissionMode.ASK, rewritten)

    assert original_evaluation.binding != rewritten_evaluation.binding
    assert rewritten_evaluation.intent.targets == (str((workspace / "after.txt").resolve()),)


@pytest.mark.parametrize(
    "command",
    (
        'python -c "import os; os.chdir(chr(46) * 2)"',
        "pytest",
        "mypy src",
        "ruff check .",
    ),
)
def test_auto_treats_code_executors_and_plugin_runners_as_unknown(
    tmp_path: Path,
    command: str,
) -> None:
    policy = PermissionPolicy(tmp_path)

    evaluation = policy.evaluate(
        PermissionMode.AUTO,
        ToolCall("shell", "bash", {"command": command}),
    )

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind.value == "unknown"
    assert evaluation.intent.scope is TargetScope.UNKNOWN


@pytest.mark.parametrize("command", ("git status", "git diff", "git log", "git show HEAD"))
def test_auto_requires_host_confirmation_for_git_commands(
    tmp_path: Path,
    command: str,
) -> None:
    evaluation = PermissionPolicy(tmp_path).evaluate(
        PermissionMode.AUTO,
        ToolCall("git", "bash", {"command": command}),
    )

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind.value == "unknown"
    assert evaluation.intent.scope is TargetScope.UNKNOWN


def test_auto_does_not_lose_windows_backslashes_during_shell_classification(
    tmp_path: Path,
) -> None:
    policy = PermissionPolicy(tmp_path)

    evaluation = policy.evaluate(
        PermissionMode.AUTO,
        ToolCall("shell", "bash", {"command": r"findstr needle C:\Windows\win.ini"}),
    )

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind.value == "unknown"
    assert evaluation.intent.scope is TargetScope.UNKNOWN


@pytest.mark.parametrize(
    "command",
    (
        "git show --output=../outside.patch HEAD",
        "grep -f../outside-patterns sample.py",
        "type %WINDIR%/win.ini",
        'cat "$HOME/.ssh/config"',
    ),
)
def test_auto_denies_path_options_and_ambiguous_shell_expansions(
    tmp_path: Path,
    command: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = PermissionPolicy(workspace)

    evaluation = policy.evaluate(
        PermissionMode.AUTO,
        ToolCall("shell", "bash", {"command": command}),
    )

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind.value == "unknown"
    assert evaluation.intent.scope is TargetScope.UNKNOWN


def test_auto_requires_permission_for_outside_output_option_before_shell_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.patch"
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))
    call = ToolCall(
        "outside-option",
        "bash",
        {"command": "git show --output=../outside.patch HEAD"},
    )

    result = asyncio.run(
        runtime.execute_guarded_batch((call,), permission_mode=PermissionMode.AUTO)
    ).results[0]

    assert result.error is not None
    assert result.error.code == "permission_required"
    assert not outside.exists()


@pytest.mark.parametrize("option", ("--pre", "--hostname-bin"))
def test_auto_requires_permission_for_shell_options_that_spawn_child_processes(
    tmp_path: Path,
    option: str,
) -> None:
    call = ToolCall(
        "child-process-option",
        "bash",
        {"command": f"rg {option} processor needle ."},
    )

    evaluation = PermissionPolicy(tmp_path).evaluate(PermissionMode.AUTO, call)

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind is OperationKind.UNKNOWN


@pytest.mark.parametrize(
    "command",
    (
        "./cat input.txt",
        "find . -exec malicious-script {} +",
        "find . -execdir malicious-script {} +",
        "find . -ok malicious-script {} +",
        "find . -okdir malicious-script {} +",
        "cat *",
        "cat file?.txt",
        "cat [ab]",
        "cat file\nunknown-command",
        "cat file\r\nunknown-command",
    ),
)
def test_auto_requires_permission_for_shell_execution_and_expansion_escapes(
    tmp_path: Path,
    command: str,
) -> None:
    evaluation = PermissionPolicy(tmp_path).evaluate(
        PermissionMode.AUTO,
        ToolCall("shell-escape", "bash", {"command": command}),
    )

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind is OperationKind.UNKNOWN


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe single-quote semantics are Windows-only")
@pytest.mark.parametrize(
    "command",
    ("echo 'safe & unknown-command'", "echo 'safe | unknown-command'"),
)
def test_auto_uses_windows_quote_semantics_for_shell_separators(
    tmp_path: Path,
    command: str,
) -> None:
    evaluation = PermissionPolicy(tmp_path).evaluate(
        PermissionMode.AUTO,
        ToolCall("windows-quote", "bash", {"command": command}),
    )

    assert evaluation.action is PermissionAction.ASK
    assert evaluation.intent.kind is OperationKind.UNKNOWN


def test_permission_request_id_is_not_reused_after_session_resume(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    first_kernel = AgentKernel.with_new_session(
        FakeProvider(
            (
                (
                    *_tool_call_events(
                        0,
                        "same-call",
                        "write",
                        {"path": "same.txt", "content": "must-not-write"},
                    ),
                    ProviderDone("tool_use"),
                ),
            )
        ),
        store,
        configuration={"provider": "fake"},
        session_id="request-replay",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def cancel_first_request() -> str:
        run = first_kernel.create_run("first request", permission_mode=PermissionMode.ASK)
        request_id = ""
        async for event in run:
            if event.permission_request is not None:
                request_id = event.permission_request.request_id
                await run.cancel()
        assert (await run.result()).state is AgentRunState.CANCELLED
        return request_id

    stale_request_id = asyncio.run(cancel_first_request())
    first_kernel.close_session()
    resumed = AgentKernel.with_resumed_session(
        FakeProvider(
            (
                (
                    *_tool_call_events(
                        0,
                        "same-call",
                        "write",
                        {"path": "same.txt", "content": "must-not-write"},
                    ),
                    ProviderDone("tool_use"),
                ),
                (ProviderDone(),),
            )
        ),
        store,
        "request-replay",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def reject_stale_request() -> None:
        run = resumed.create_run("same request", permission_mode=PermissionMode.ASK)
        async for event in run:
            if event.permission_request is None:
                continue
            assert event.permission_request.request_id != stale_request_id
            with pytest.raises(RuntimeError, match="stale|match"):
                await run.resolve_permission(stale_request_id, True)
            await run.resolve_permission(event.permission_request.request_id, False)
        assert (await run.result()).state is AgentRunState.SETTLED

    asyncio.run(reject_stale_request())

    assert stale_request_id
    assert not (tmp_path / "same.txt").exists()


def test_tool_runtime_denial_is_side_effect_free_and_scheduling_is_orthogonal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))
    calls = (
        ToolCall("read", "read", {"path": "value.txt"}),
        ToolCall("write", "write", {"path": "value.txt", "content": "after"}),
    )

    batch = asyncio.run(runtime.execute_guarded_batch(calls, permission_mode=PermissionMode.PLAN))

    assert batch.mode == "sequential"
    assert [result.status for result in batch.results] == ["success", "error"]
    assert batch.results[1].error is not None
    assert batch.results[1].error.code == "permission_denied"
    assert target.read_text(encoding="utf-8") == "before"


def test_guarded_runtime_normalizes_a_host_mode_string_before_binding(
    tmp_path: Path,
) -> None:
    target = tmp_path / "approved.txt"
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    call = ToolCall("approved", "write", {"path": "approved.txt", "content": "value"})
    evaluation = PermissionPolicy(tmp_path).evaluate(PermissionMode.ASK, call)
    decision = PermissionDecision(
        call.call_id,
        call.tool_name,
        PermissionMode.ASK,
        "approved",
        "host",
        evaluation.final_arguments_json,
        evaluation.intent,
        evaluation.binding,
        "Host approved the one-time Permission Request",
    )

    batch = asyncio.run(
        runtime.execute_guarded_batch(
            (call,),
            permission_mode="ask",
            permission_decisions={call.call_id: decision},
        )
    )

    assert batch.results[0].status == "success"
    assert target.read_text(encoding="utf-8") == "value"


def test_run_local_execution_view_preserves_environment_adapter_state(
    tmp_path: Path,
) -> None:
    (tmp_path / "value.txt").write_text("value", encoding="utf-8")
    runtime = ToolRuntime(_StatefulEnvironment(tmp_path))

    batch = asyncio.run(
        runtime.execute_guarded_batch(
            (ToolCall("read", "read", {"path": "value.txt"}),),
            permission_mode=PermissionMode.AUTO,
        )
    )

    assert batch.results[0].status == "success"
    assert batch.results[0].output == {"content": "value"}


def test_permission_classification_failure_is_a_model_visible_tool_error(
    tmp_path: Path,
) -> None:
    environment = _ExecutionRecordingEnvironment(tmp_path)
    provider = FakeProvider(
        (
            (
                *_tool_call_events(0, "invalid-path", "read", {"path": "\0"}),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(environment),
    )

    async def scenario() -> tuple[list[AgentSessionEvent], str]:
        run = kernel.create_run("read an invalid path")
        events = [event async for event in run]
        result = await run.result()
        return events, result.state.value

    events, state = asyncio.run(scenario())
    tool_result = next(
        event.tool_result
        for event in events
        if event.kind is AgentSessionEventKind.TOOL_EXECUTION_END and event.tool_result is not None
    )

    assert state == "settled"
    assert tool_result.error is not None
    assert tool_result.error.code == "permission_invalid"
    assert environment.read_paths == []
    feedback = cast(ToolResultMessage, provider.requests[1].messages[-1])
    assert feedback.results == (tool_result,)


def test_full_skips_kernel_containment_but_auto_does_not(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))
    call = ToolCall("outside", "write", {"path": "../outside.txt", "content": "created"})

    auto = asyncio.run(runtime.execute_guarded_batch((call,), permission_mode=PermissionMode.AUTO))
    full = asyncio.run(runtime.execute_guarded_batch((call,), permission_mode=PermissionMode.FULL))

    assert auto.results[0].error is not None
    assert auto.results[0].error.code == "permission_required"
    assert full.results[0].status == "success"
    assert outside.read_text(encoding="utf-8") == "created"


def test_full_search_tools_can_report_outside_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "needle.py"
    target.write_text("needle = 1\n", encoding="utf-8")
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))
    runtime.enable("grep", "find")
    calls = (
        ToolCall("grep", "grep", {"pattern": "needle", "path": "../outside"}),
        ToolCall("find", "find", {"pattern": "*.py", "path": "../outside"}),
    )

    batch = asyncio.run(runtime.execute_guarded_batch(calls, permission_mode=PermissionMode.FULL))

    assert [result.status for result in batch.results] == ["success", "success"]
    assert batch.results[0].output == {
        "matches": [{"path": str(target.resolve()), "line": 1, "text": "needle = 1"}]
    }
    assert batch.results[1].output == {"paths": [str(target.resolve())]}


def test_non_full_search_does_not_follow_a_directory_link_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = workspace / "linked-outside"
    _create_directory_link(link, outside)
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))
    runtime.enable("find")

    batch = asyncio.run(
        runtime.execute_guarded_batch(
            (ToolCall("find", "find", {"pattern": "*.txt", "path": "."}),),
            permission_mode=PermissionMode.AUTO,
        )
    )

    assert batch.results[0].status == "success"
    assert batch.results[0].output == {"paths": []}


def test_auto_requires_host_confirmation_for_a_bare_shell_target_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    _create_directory_link(workspace / "linked-outside", outside)
    command = "dir linked-outside" if os.name == "nt" else "ls linked-outside"
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))

    result = asyncio.run(
        runtime.execute_guarded_batch(
            (ToolCall("shell", "bash", {"command": command}),),
            permission_mode=PermissionMode.AUTO,
        )
    ).results[0]

    assert result.error is not None
    assert result.error.code == "permission_required"


def test_agent_run_resolves_each_permission_once_and_denial_reaches_the_model(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "approved-write",
                    "write",
                    {"path": "approved.txt", "content": "approved"},
                ),
                *_tool_call_events(
                    1,
                    "denied-write",
                    "write",
                    {"path": "denied.txt", "content": "denied"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def scenario() -> list[AgentSessionEvent]:
        run = kernel.create_run("write two files", permission_mode=PermissionMode.ASK)
        assert run.permission_mode is PermissionMode.ASK
        events: list[AgentSessionEvent] = []
        async for event in run:
            events.append(event)
            if event.kind is AgentSessionEventKind.PERMISSION_REQUESTED:
                request = event.permission_request
                assert request is not None
                assert request.mode is PermissionMode.ASK
                approved = request.call_id == "approved-write"
                await run.resolve_permission(request.request_id, approved)
                with pytest.raises(RuntimeError, match="pending|resolved"):
                    await run.resolve_permission(request.request_id, approved)
        await run.result()
        return events

    events = asyncio.run(scenario())

    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved"
    assert not (tmp_path / "denied.txt").exists()
    requested = [
        event.permission_request
        for event in events
        if event.kind is AgentSessionEventKind.PERMISSION_REQUESTED
    ]
    resolved = [
        event for event in events if event.kind is AgentSessionEventKind.PERMISSION_RESOLVED
    ]
    assert len(requested) == len(resolved) == 2
    assert [event.permission_request_id for event in resolved] == [
        request.request_id for request in requested if request is not None
    ]
    denied_results = [
        event.tool_result
        for event in events
        if event.kind is AgentSessionEventKind.TOOL_EXECUTION_END
        and event.tool_result is not None
        and event.tool_result.call_id == "denied-write"
    ]
    assert denied_results[0].error is not None
    assert denied_results[0].error.code == "permission_denied"
    feedback = cast(ToolResultMessage, provider.requests[1].messages[-1])
    assert feedback.results[1] == denied_results[0]


def test_policy_decisions_do_not_emit_permission_resolved(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "automatic-write",
                    "write",
                    {"path": "automatic.txt", "content": "automatic"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def scenario() -> list[AgentSessionEvent]:
        run = kernel.create_run("write automatically")
        events = [event async for event in run]
        await run.result()
        return events

    events = asyncio.run(scenario())

    assert (tmp_path / "automatic.txt").read_text(encoding="utf-8") == "automatic"
    assert all(
        event.kind
        not in {
            AgentSessionEventKind.PERMISSION_REQUESTED,
            AgentSessionEventKind.PERMISSION_RESOLVED,
        }
        for event in events
    )


def test_accepted_permission_resolution_is_published_before_immediate_cancel(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "resolved-before-cancel",
                    "write",
                    {"path": "not-executed.txt", "content": "blocked by cancellation"},
                ),
                ProviderDone("tool_use"),
            ),
        )
    )
    store = InMemorySessionStore()
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        configuration={"provider": "fake"},
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def scenario() -> tuple[AgentRunState, list[AgentSessionEvent]]:
        run = kernel.create_run("resolve then cancel", permission_mode=PermissionMode.ASK)
        events: list[AgentSessionEvent] = []
        async for event in run:
            events.append(event)
            if event.permission_request is not None:
                await run.resolve_permission(event.permission_request.request_id, True)
                await run.cancel()
        return (await run.result()).state, events

    state, events = asyncio.run(scenario())

    assert state is AgentRunState.CANCELLED
    assert not (tmp_path / "not-executed.txt").exists()
    resolved = [
        event for event in events if event.kind is AgentSessionEventKind.PERMISSION_RESOLVED
    ]
    assert len(resolved) == 1
    assert resolved[0].permission_decision is not None
    assert resolved[0].permission_decision.resolution == "approved"
    decisions = [
        entry for entry in kernel.session_active_branch if entry.kind == "permission_decision"
    ]
    assert len(decisions) == 1
    assert decisions[0].payload["resolution"] == "approved"
    assert next(
        index
        for index, event in enumerate(events)
        if event.kind is AgentSessionEventKind.PERMISSION_RESOLVED
    ) < next(
        index
        for index, event in enumerate(events)
        if event.kind is AgentSessionEventKind.RUN_CANCELLED
    )


def test_resolved_decision_is_durable_without_pending_capability_and_full_downgrades(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "durable-write",
                    "write",
                    {"path": "durable.txt", "content": "durable"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
            (ProviderDone(),),
        )
    )
    store = InMemorySessionStore()
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        configuration={"provider": "fake"},
        session_id="permission-session",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def approve() -> None:
        run = kernel.create_run("persist decision", permission_mode=PermissionMode.ASK)
        async for event in run:
            if event.permission_request is not None:
                await run.resolve_permission(event.permission_request.request_id, True)
        await run.result()

    asyncio.run(approve())
    decisions = [
        entry for entry in kernel.session_active_branch if entry.kind == "permission_decision"
    ]

    assert len(decisions) == 1
    payload = decisions[0].payload
    assert payload["call_id"] == "durable-write"
    assert payload["resolution"] == "approved"
    assert payload["source"] == "host"
    assert "request_id" not in payload
    assert "binding" not in payload
    assert "permission:" not in decisions[0].payload_json

    async def select_full_before_close() -> PermissionMode:
        run = kernel.create_run("explicit trusted run", permission_mode=PermissionMode.FULL)
        mode = run.permission_mode
        async for _ in run:
            pass
        await run.result()
        return mode

    assert asyncio.run(select_full_before_close()) is PermissionMode.FULL

    kernel.close_session()
    resumed = AgentKernel.with_resumed_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        "permission-session",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def resumed_mode() -> PermissionMode:
        run = resumed.create_run("resume defaults safely")
        mode = run.permission_mode
        async for _ in run:
            pass
        await run.result()
        return mode

    assert asyncio.run(resumed_mode()) is PermissionMode.AUTO


@pytest.mark.parametrize("termination", ("cancel", "disconnect"))
def test_pending_permission_is_invalidated_by_cancel_or_host_disconnect(
    tmp_path: Path,
    termination: str,
) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "never-write",
                    "write",
                    {"path": "never.txt", "content": "must not exist"},
                ),
                ProviderDone("tool_use"),
            ),
        )
    )
    store = InMemorySessionStore()
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        configuration={"provider": "fake"},
        session_id=f"pending-{termination}",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def scenario() -> tuple[str, str, list[AgentSessionEvent]]:
        run = kernel.create_run("wait for Host", permission_mode=PermissionMode.ASK)
        request_id = ""
        events: list[AgentSessionEvent] = []
        async for event in run:
            events.append(event)
            if event.permission_request is not None:
                request_id = event.permission_request.request_id
                if termination == "cancel":
                    await run.cancel()
                else:
                    await run.aclose()
        result = await run.result()
        with pytest.raises(RuntimeError, match="not active|pending"):
            await run.resolve_permission(request_id, True)
        return result.state.value, request_id, events

    state, request_id, events = asyncio.run(scenario())

    assert state == "cancelled"
    assert request_id
    assert not (tmp_path / "never.txt").exists()
    resolved = [
        event for event in events if event.kind is AgentSessionEventKind.PERMISSION_RESOLVED
    ]
    assert len(resolved) == 1
    assert resolved[0].permission_request_id == request_id
    assert resolved[0].permission_decision is not None
    assert resolved[0].permission_decision.resolution == "denied"
    decisions = [
        entry for entry in kernel.session_active_branch if entry.kind == "permission_decision"
    ]
    assert len(decisions) == 1
    assert decisions[0].payload["resolution"] == "denied"
    assert decisions[0].payload["source"] == "host"
    assert "request_id" not in decisions[0].payload
    assert "binding" not in decisions[0].payload


def test_extension_rewrite_invalidates_pre_hook_approval_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "rewritten-write",
                    "write",
                    {"path": "before.txt", "content": "rewritten"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    policy = PermissionPolicy(workspace)
    stale = policy.evaluate(
        PermissionMode.ASK,
        ToolCall(
            "rewritten-write",
            "write",
            {"path": "before.txt", "content": "rewritten"},
        ),
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
        extensions=(_RewritePathExtension("after.txt"),),
    )

    async def scenario() -> None:
        run = kernel.create_run("rewrite then approve", permission_mode=PermissionMode.ASK)
        async for event in run:
            request = event.permission_request
            if request is None:
                continue
            assert request.final_arguments["path"] == "after.txt"
            stale_request_id = f"{request.run_id}:permission:1:{stale.binding[:16]}"
            with pytest.raises(RuntimeError, match="stale|match"):
                await run.resolve_permission(stale_request_id, True)
            await run.resolve_permission(request.request_id, True)
        await run.result()

    asyncio.run(scenario())

    assert not (workspace / "before.txt").exists()
    assert (workspace / "after.txt").read_text(encoding="utf-8") == "rewritten"


def test_host_can_deny_an_auto_request_after_an_extension_rewrites_outside(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "outside-write",
                    "write",
                    {"path": "inside.txt", "content": "blocked"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
        extensions=(_RewritePathExtension("../outside.txt"),),
    )

    async def scenario() -> list[AgentSessionEvent]:
        run = kernel.create_run("rewrite outside")
        events: list[AgentSessionEvent] = []
        async for event in run:
            events.append(event)
            if event.permission_request is not None:
                assert event.permission_request.mode is PermissionMode.AUTO
                await run.resolve_permission(event.permission_request.request_id, False)
        await run.result()
        return events

    events = asyncio.run(scenario())
    result = next(
        event.tool_result
        for event in events
        if event.kind is AgentSessionEventKind.TOOL_EXECUTION_END and event.tool_result is not None
    )

    assert result.error is not None
    assert result.error.code == "permission_denied"
    assert not outside.exists()


def test_session_resume_rejects_permission_decision_with_pending_capability(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "session.jsonl"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "durable-write",
                    "write",
                    {"path": "durable.txt", "content": "not-in-decision"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    store = JsonlSessionStore(session_path)
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        configuration={"provider": "fake"},
        session_id="corrupt-permission",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
    )

    async def approve() -> None:
        run = kernel.create_run("persist", permission_mode=PermissionMode.ASK)
        async for event in run:
            if event.permission_request is not None:
                await run.resolve_permission(event.permission_request.request_id, True)
        await run.result()

    asyncio.run(approve())
    kernel.close_session()
    records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
    decision_record = next(
        record
        for record in records
        if record.get("record_type") == "entry"
        and record["entry"].get("kind") == "permission_decision"
    )
    decision_record["entry"]["payload"]["request_id"] = "stale-capability"
    session_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )

    with pytest.raises(SessionCorruptionError, match="Permission Decision"):
        AgentKernel.with_resumed_session(
            FakeProvider(((ProviderDone(),),)),
            JsonlSessionStore(session_path),
            "corrupt-permission",
            tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
        )


@pytest.mark.parametrize(
    "corruption",
    (
        "empty-targets",
        "empty-intent-reason",
        "file-command-digest",
        "write-read-kind",
        "ask-policy-write-approved",
    ),
)
def test_session_resume_rejects_semantically_invalid_permission_decision(
    tmp_path: Path,
    corruption: str,
) -> None:
    session_path = tmp_path / "session.jsonl"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = FakeProvider(
        (
            (
                *_tool_call_events(
                    0,
                    "durable-write",
                    "write",
                    {"path": "durable.txt", "content": "not-in-decision"},
                ),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel.with_new_session(
        provider,
        JsonlSessionStore(session_path),
        configuration={"provider": "fake"},
        session_id=f"corrupt-{corruption}",
        tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
    )

    async def approve() -> None:
        run = kernel.create_run("persist", permission_mode=PermissionMode.ASK)
        async for event in run:
            if event.permission_request is not None:
                await run.resolve_permission(event.permission_request.request_id, True)
        await run.result()

    asyncio.run(approve())
    kernel.close_session()
    records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
    decision = next(
        record["entry"]["payload"]
        for record in records
        if record.get("record_type") == "entry"
        and record["entry"].get("kind") == "permission_decision"
    )
    intent = decision["operation_intent"]
    if corruption == "empty-targets":
        intent["targets"] = []
    elif corruption == "empty-intent-reason":
        intent["reason"] = ""
    elif corruption == "file-command-digest":
        intent["command_sha256"] = "0" * 64
    elif corruption == "write-read-kind":
        intent["kind"] = "read"
    else:
        decision["source"] = "policy"
    session_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )

    with pytest.raises(SessionCorruptionError, match="Permission Decision"):
        AgentKernel.with_resumed_session(
            FakeProvider(((ProviderDone(),),)),
            JsonlSessionStore(session_path),
            f"corrupt-{corruption}",
            tool_runtime=ToolRuntime(LocalCodingEnvironment(workspace)),
        )
