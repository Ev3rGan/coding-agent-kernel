"""Local disposable-workspace implementation of the CodingEnvironment seam."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


class WorkspacePathError(ValueError):
    """A requested path escaped the configured workspace."""


class CommandTimeoutError(TimeoutError):
    """A command exceeded its requested timeout."""


@dataclass(frozen=True, slots=True)
class ProcessChunk:
    stream: str
    data: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    chunks: tuple[ProcessChunk, ...]


ChunkCallback = Callable[[ProcessChunk], Awaitable[None]]


def raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
    """Raise at a cooperative cancellation point shared by local Tools."""

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError


class LocalCodingEnvironment:
    """File and process operations contained to a disposable local workspace.

    This is an execution boundary, not a production sandbox or a security claim
    for hostile workspaces.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._contain_workspace = True

    def _execution_view(self, *, contain_workspace: bool) -> LocalCodingEnvironment:
        """Return a run-local view without mutating a shared environment."""

        view = object.__new__(type(self))
        view.workspace = self.workspace
        view._contain_workspace = contain_workspace
        return view

    def resolve_path(self, path: str) -> Path:
        candidate = (self.workspace / path).resolve()
        if (
            self._contain_workspace
            and candidate != self.workspace
            and self.workspace not in candidate.parents
        ):
            raise WorkspacePathError(f"path escapes workspace: {path}")
        return candidate

    async def read_text(self, path: str, cancel_event: asyncio.Event | None = None) -> str:
        raise_if_cancelled(cancel_event)
        content = await asyncio.to_thread(self.resolve_path(path).read_text, encoding="utf-8")
        raise_if_cancelled(cancel_event)
        return content

    async def write_text(
        self, path: str, content: str, cancel_event: asyncio.Event | None = None
    ) -> int:
        raise_if_cancelled(cancel_event)
        target = self.resolve_path(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        raise_if_cancelled(cancel_event)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        write_task = asyncio.create_task(
            asyncio.to_thread(temporary.write_text, content, encoding="utf-8")
        )
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            await write_task
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
            raise
        if cancel_event is not None and cancel_event.is_set():
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
            raise asyncio.CancelledError
        temporary.replace(target)
        return len(content.encode("utf-8"))

    async def edit_text(
        self,
        path: str,
        old: str,
        new: str,
        cancel_event: asyncio.Event | None = None,
    ) -> int:
        content = await self.read_text(path, cancel_event)
        count = content.count(old)
        if count == 0:
            raise ValueError("old text was not found")
        await self.write_text(path, content.replace(old, new), cancel_event)
        return count

    async def run_command(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
        cancel_event: asyncio.Event | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> ProcessResult:
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.resolve_path(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.resolve_path(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        chunks: list[ProcessChunk] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        async def drain(reader: asyncio.StreamReader | None, stream: str, parts: list[str]) -> None:
            if reader is None:
                return
            while data := await reader.read(4096):
                text = data.decode("utf-8", errors="replace")
                parts.append(text)
                chunk = ProcessChunk(stream, text)
                chunks.append(chunk)
                if on_chunk is not None:
                    await on_chunk(chunk)

        stdout_task = asyncio.create_task(drain(process.stdout, "stdout", stdout_parts))
        stderr_task = asyncio.create_task(drain(process.stderr, "stderr", stderr_parts))
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            waiters = {wait_task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(
                waiters, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task is not None and cancel_task in done:
                await self._terminate_process_tree(process)
                raise asyncio.CancelledError
            if wait_task not in done:
                await self._terminate_process_tree(process)
                raise CommandTimeoutError(f"command timed out after {timeout_seconds:g}s")
            await asyncio.gather(stdout_task, stderr_task)
            return ProcessResult(
                exit_code=wait_task.result(),
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                chunks=tuple(chunks),
            )
        except asyncio.CancelledError:
            await self._terminate_process_tree(process)
            raise
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
            for task in (stdout_task, stderr_task, wait_task):
                if not task.done():
                    task.cancel()

    @staticmethod
    async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        await process.wait()
