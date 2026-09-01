from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

import pytest

from coding_agent import LocalCodingEnvironment, ToolBatchResult, ToolCall, ToolRuntime
from coding_agent.environment import (
    _kill_process_group,
    _windows_process_group_creation_flags,
)


class _ReadBFirstEnvironment(LocalCodingEnvironment):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._read_b_completed = asyncio.Event()

    async def read_text(
        self,
        path: str,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        if path == "a.txt":
            await self._read_b_completed.wait()
        content = await super().read_text(path, cancel_event)
        if path == "b.txt":
            self._read_b_completed.set()
        return content


def test_process_group_helpers_bind_platform_apis_with_explicit_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    calls: list[tuple[int, int]] = []

    def record_kill(process_group_id: int, signal_number: int) -> None:
        calls.append((process_group_id, signal_number))

    monkeypatch.setattr(os, "killpg", record_kill, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    assert _windows_process_group_creation_flags() == 512
    _kill_process_group(123)

    assert calls == [(123, 9)]


def test_process_group_helpers_fail_closed_when_platform_apis_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)
    monkeypatch.delattr(os, "killpg", raising=False)

    with pytest.raises(RuntimeError, match="CREATE_NEW_PROCESS_GROUP is unavailable"):
        _windows_process_group_creation_flags()
    with pytest.raises(RuntimeError, match="os.killpg is unavailable"):
        _kill_process_group(123)

    monkeypatch.setattr(os, "killpg", lambda process_group_id, signal_number: None, raising=False)
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    with pytest.raises(RuntimeError, match="signal.SIGKILL is unavailable"):
        _kill_process_group(123)


def test_environment_rejects_nul_path_before_normalization(tmp_path: Path) -> None:
    environment = LocalCodingEnvironment(tmp_path)

    with pytest.raises(ValueError, match="NUL"):
        environment.resolve_path("target.txt\0../../outside.txt")


def test_runtime_registers_seven_tools_and_requires_opt_in_for_search_tools(tmp_path: Path) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))

    assert runtime.registered_names == ("bash", "edit", "find", "grep", "ls", "read", "write")
    assert runtime.enabled_names == ("bash", "edit", "read", "write")

    result = asyncio.run(
        runtime.execute_batch((ToolCall("call-1", "grep", {"pattern": "x"}),))
    ).results[0]
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_disabled"


def test_mixed_batch_is_sequential_and_results_keep_call_order(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "note.txt").write_text("before\n", encoding="utf-8")
    runtime = ToolRuntime(LocalCodingEnvironment(workspace))
    calls = (
        ToolCall("read-first", "read", {"path": "note.txt"}),
        ToolCall("write-second", "write", {"path": "note.txt", "content": "after\n"}),
        ToolCall("read-third", "read", {"path": "note.txt"}),
    )

    batch = asyncio.run(runtime.execute_batch(calls))

    assert batch.mode == "sequential"
    assert batch.completion_order == ("read-first", "write-second", "read-third")
    assert tuple(result.call_id for result in batch.results) == (
        "read-first",
        "write-second",
        "read-third",
    )
    assert batch.results[0].output == {"content": "before\n"}
    assert batch.results[2].output == {"content": "after\n"}


def test_pure_read_batch_is_parallel_but_model_results_keep_call_order(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "a.txt").write_text("A", encoding="utf-8")
    (workspace / "b.txt").write_text("B", encoding="utf-8")

    async def execute_batch() -> ToolBatchResult:
        runtime = ToolRuntime(_ReadBFirstEnvironment(workspace))
        return await runtime.execute_batch(
            (
                ToolCall("read-a", "read", {"path": "a.txt"}),
                ToolCall("read-b", "read", {"path": "b.txt"}),
            )
        )

    batch = asyncio.run(execute_batch())

    assert batch.mode == "parallel"
    assert batch.completion_order == ("read-b", "read-a")
    assert tuple(result.call_id for result in batch.results) == ("read-a", "read-b")
    assert tuple(result.output for result in batch.results) == (
        {"content": "A"},
        {"content": "B"},
    )


def test_runtime_normalizes_unknown_invalid_process_timeout_and_cancel(tmp_path: Path) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    cancelled = asyncio.Event()
    cancelled.set()
    cases = (
        ToolCall("unknown", "missing", {}),
        ToolCall("invalid", "read", {}),
        ToolCall("bool-timeout", "bash", {"command": "exit 0", "timeout": True}),
        ToolCall("failed", "bash", {"command": "exit 7"}),
        ToolCall(
            "timeout",
            "bash",
            {"command": 'python -c "import time; time.sleep(2)"', "timeout": 0.01},
        ),
    )

    batch = asyncio.run(runtime.execute_batch(cases))
    cancelled_batch = asyncio.run(
        runtime.execute_batch((ToolCall("cancelled", "read", {"path": "none"}),), cancelled)
    )

    assert [result.error.code if result.error else None for result in batch.results] == [
        "unknown_tool",
        "invalid_arguments",
        "invalid_arguments",
        "process_failed",
        "timeout",
    ]
    assert batch.results[3].output is not None
    assert batch.results[3].output["exit_code"] == 7
    assert cancelled_batch.results[0].status == "cancelled"
    assert cancelled_batch.results[0].error is not None
    assert cancelled_batch.results[0].error.code == "cancelled"


def test_opt_in_search_tools_execute_against_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("needle = 1\n", encoding="utf-8")
    (tmp_path / "src" / "two.txt").write_text("other\n", encoding="utf-8")
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    runtime.enable("grep", "find", "ls")

    batch = asyncio.run(
        runtime.execute_batch(
            (
                ToolCall("grep", "grep", {"pattern": "needle", "path": "src"}),
                ToolCall("find", "find", {"pattern": "*.py", "path": "src"}),
                ToolCall("ls", "ls", {"path": "src"}),
            )
        )
    )

    assert batch.mode == "parallel"
    assert batch.results[0].output == {
        "matches": [{"path": "src/one.py", "line": 1, "text": "needle = 1"}]
    }
    assert batch.results[1].output == {"paths": ["src/one.py"]}
    assert batch.results[2].output == {
        "entries": [
            {"name": "one.py", "type": "file"},
            {"name": "two.txt", "type": "file"},
        ]
    }


def test_search_tool_observes_cancellation_while_scanning(tmp_path: Path) -> None:
    for index in range(200):
        (tmp_path / f"file-{index}.txt").write_text("content\n" * 20, encoding="utf-8")
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    runtime.enable("grep")

    async def cancel_search() -> ToolBatchResult:
        cancelled = asyncio.Event()
        task = asyncio.create_task(
            runtime.execute_batch(
                (ToolCall("grep", "grep", {"pattern": "missing", "path": "."}),),
                cancelled,
            )
        )
        await asyncio.sleep(0.01)
        cancelled.set()
        return await task

    batch = asyncio.run(cancel_search())
    assert batch.results[0].status == "cancelled"


def test_cancelled_write_does_not_replace_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))

    async def cancel_write() -> ToolBatchResult:
        cancelled = asyncio.Event()
        task = asyncio.create_task(
            runtime.execute_batch(
                (
                    ToolCall(
                        "write",
                        "write",
                        {"path": "target.txt", "content": "replacement" * 1_000_000},
                    ),
                ),
                cancelled,
            )
        )
        await asyncio.sleep(0)
        cancelled.set()
        return await task

    batch = asyncio.run(cancel_write())
    assert batch.results[0].status == "cancelled"
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".*.tmp")) == []
