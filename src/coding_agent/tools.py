"""Seven built-in coding tools and their structured contracts."""

from __future__ import annotations

import asyncio
import fnmatch
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from coding_agent.environment import LocalCodingEnvironment, ProcessChunk, raise_if_cancelled

ToolMode = Literal["parallel", "sequential"]
ToolProgressCallback = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    mode: ToolMode
    enabled_by_default: bool


@dataclass(frozen=True, slots=True)
class ToolOutput:
    data: dict[str, Any]


class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput: ...


class ToolExecutionError(Exception):
    """A Tool-owned failure carrying model-visible structured output."""

    def __init__(self, code: str, message: str, output: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.output = output


def _schema(required: tuple[str, ...], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(required),
        "properties": properties,
        "additionalProperties": False,
    }


class ReadTool:
    spec = ToolSpec(
        "read",
        "Read a UTF-8 text file from the workspace.",
        _schema(("path",), {"path": {"type": "string"}}),
        "parallel",
        True,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del on_progress
        return ToolOutput({"content": await environment.read_text(arguments["path"], cancel_event)})


class WriteTool:
    spec = ToolSpec(
        "write",
        "Write a UTF-8 text file in the workspace.",
        _schema(("path", "content"), {"path": {"type": "string"}, "content": {"type": "string"}}),
        "sequential",
        True,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del on_progress
        size = await environment.write_text(arguments["path"], arguments["content"], cancel_event)
        return ToolOutput({"bytes_written": size})


class EditTool:
    spec = ToolSpec(
        "edit",
        "Replace exact text in a UTF-8 workspace file.",
        _schema(
            ("path", "old", "new"), {key: {"type": "string"} for key in ("path", "old", "new")}
        ),
        "sequential",
        True,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del on_progress
        replacements = await environment.edit_text(
            arguments["path"], arguments["old"], arguments["new"], cancel_event
        )
        return ToolOutput({"replacements": replacements})


class BashTool:
    spec = ToolSpec(
        "bash",
        "Run a shell command in the workspace and capture output.",
        _schema(
            ("command",),
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number", "minimum": 0},
            },
        ),
        "sequential",
        True,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        async def forward_chunk(chunk: ProcessChunk) -> None:
            if on_progress is not None:
                await on_progress(chunk.stream, chunk.data)

        result = await environment.run_command(
            arguments["command"],
            cwd=arguments.get("cwd", "."),
            timeout_seconds=float(arguments.get("timeout", 30.0)),
            cancel_event=cancel_event,
            on_chunk=forward_chunk,
        )
        output = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "chunks": [{"stream": chunk.stream, "data": chunk.data} for chunk in result.chunks],
        }
        if result.exit_code != 0:
            raise ToolExecutionError(
                "process_failed",
                f"command exited with status {result.exit_code}",
                output,
            )
        return ToolOutput(output)


async def _files(
    root: Path,
    environment: LocalCodingEnvironment,
    cancel_event: asyncio.Event | None,
) -> list[Path]:
    raise_if_cancelled(cancel_event)

    def scan() -> list[Path]:
        directories = [root]
        visited: set[Path] = set()
        paths: list[Path] = []
        while directories:
            candidate = directories.pop()
            try:
                directory = environment.resolve_path(str(candidate))
                if directory in visited:
                    continue
                visited.add(directory)
                entries = sorted(directory.iterdir(), reverse=True)
            except (OSError, ValueError):
                continue
            for entry in entries:
                try:
                    resolved = environment.resolve_path(str(entry))
                    if resolved.is_dir():
                        directories.append(resolved)
                    elif resolved.is_file():
                        paths.append(resolved)
                except (OSError, ValueError):
                    continue
        return sorted(paths)

    paths = await asyncio.to_thread(scan)
    raise_if_cancelled(cancel_event)
    return paths


def _display_path(path: Path, environment: LocalCodingEnvironment) -> str:
    try:
        return path.relative_to(environment.workspace).as_posix()
    except ValueError:
        return str(path)


class GrepTool:
    spec = ToolSpec(
        "grep",
        "Search UTF-8 workspace files with a regular expression.",
        _schema(("pattern",), {"pattern": {"type": "string"}, "path": {"type": "string"}}),
        "parallel",
        False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del on_progress
        root = environment.resolve_path(arguments.get("path", "."))
        pattern = re.compile(arguments["pattern"])
        matches: list[dict[str, Any]] = []
        candidates = await _files(root, environment, cancel_event) if root.is_dir() else [root]
        for path in candidates:
            raise_if_cancelled(cancel_event)
            display_path = _display_path(path, environment)
            content = await environment.read_text(str(path), cancel_event)
            for line_number, line in enumerate(content.splitlines(), 1):
                raise_if_cancelled(cancel_event)
                if pattern.search(line):
                    matches.append(
                        {
                            "path": display_path,
                            "line": line_number,
                            "text": line,
                        }
                    )
                await asyncio.sleep(0)
        return ToolOutput({"matches": matches})


class FindTool:
    spec = ToolSpec(
        "find",
        "Find workspace files matching a glob pattern.",
        _schema(("pattern",), {"pattern": {"type": "string"}, "path": {"type": "string"}}),
        "parallel",
        False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del on_progress
        root = environment.resolve_path(arguments.get("path", "."))
        paths = [
            _display_path(path, environment)
            for path in await _files(root, environment, cancel_event)
            if fnmatch.fnmatch(path.name, arguments["pattern"])
        ]
        return ToolOutput({"paths": paths})


class LsTool:
    spec = ToolSpec(
        "ls",
        "List one workspace directory.",
        _schema((), {"path": {"type": "string"}}),
        "parallel",
        False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del on_progress
        root = environment.resolve_path(arguments.get("path", "."))
        entries: list[dict[str, str]] = []
        paths = await asyncio.to_thread(lambda: sorted(root.iterdir()))
        for path in paths:
            raise_if_cancelled(cancel_event)
            entries.append({"name": path.name, "type": "directory" if path.is_dir() else "file"})
            await asyncio.sleep(0)
        return ToolOutput({"entries": entries})


def builtin_tools() -> tuple[Tool, ...]:
    return (ReadTool(), WriteTool(), EditTool(), BashTool(), GrepTool(), FindTool(), LsTool())
