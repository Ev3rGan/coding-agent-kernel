"""SWE-bench Verified evaluator Host seam."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

from coding_agent.context import ContextSettings
from coding_agent.environment import (
    ChunkCallback,
    CommandTimeoutError,
    LocalCodingEnvironment,
    ProcessChunk,
    ProcessResult,
    WorkspacePathError,
    raise_if_cancelled,
)
from coding_agent.events import AgentRunResult, AgentRunState, AgentSessionEvent
from coding_agent.kernel import AgentKernel
from coding_agent.permissions import PermissionMode
from coding_agent.provider import ModelProvider
from coding_agent.run import AgentRun
from coding_agent.session import JsonlSessionStore
from coding_agent.tool_runtime import ToolRuntime

_INSTANCE_ID = re.compile(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+")
SWE_BENCH_DATASET = "SWE-bench/SWE-bench_Verified"
SWE_BENCH_SPLIT = "test"
SWE_BENCH_CONTRACT_COMMIT = "7a21e05772954cc81471ae19d56f436cecf43c54"
SWE_BENCH_DATASET_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
SWE_BENCH_HARNESS_VERSION = "5.0.2"
_MAX_PREDICTION_PATCH_BYTES = 10 * 1024 * 1024
_CLEANUP_TIMEOUT_SECONDS = 5.0
_OFFICIAL_RUNTIME_IMPORT_LOCK = threading.Lock()
_WINDOWS_HARNESS_BOOTSTRAP = r"""
import pathlib
import runpy

_original_write_text = pathlib.Path.write_text


def _write_text(self, data, encoding=None, errors=None, newline="\n"):
    return _original_write_text(
        self, data, encoding=encoding, errors=errors, newline=newline
    )


pathlib.Path.write_text = _write_text
runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")
"""
SWEbenchStage = Literal[
    "configuration",
    "provider_configuration",
    "instance_loading",
    "environment_preparation",
    "agent",
    "prediction",
    "harness",
    "harness_result",
    "complete",
]
SWEbenchStatus = Literal[
    "running",
    "success",
    "environment_preparation_failed",
    "agent_failed",
    "timed_out",
    "cancelled",
    "no_patch",
    "prediction_invalid",
    "harness_invocation_failed",
    "harness_rejected",
    "harness_failed",
]


class SWEbenchConfigurationError(ValueError):
    """A user-provided evaluator setting is invalid."""


def validate_instance_id(instance_id: str) -> str:
    """Validate one official SWE-bench-style instance identifier."""

    if _INSTANCE_ID.fullmatch(instance_id) is None:
        raise SWEbenchConfigurationError(
            "instance ID must use the official owner__repo-number form"
        )
    return instance_id


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Bounded result from one strict-argv host process."""

    exit_code: int
    stdout: str
    stderr: str
    output_truncated: bool


class CommandRunner(Protocol):
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None,
        on_chunk: ChunkCallback | None,
        output_limit_bytes: int,
    ) -> CommandOutcome: ...


class SubprocessCommandRunner:
    """Run strict argv processes with bounded capture and whole-tree termination."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None,
        on_chunk: ChunkCallback | None,
        output_limit_bytes: int,
    ) -> CommandOutcome:
        if not argv or output_limit_bytes < 0:
            raise ValueError("process argv and output limit must be valid")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        remaining = output_limit_bytes
        truncated = False

        async def drain(
            reader: asyncio.StreamReader | None,
            stream: str,
            parts: list[str],
        ) -> None:
            nonlocal remaining, truncated
            if reader is None:
                return
            while data := await reader.read(4096):
                accepted = data[:remaining]
                remaining -= len(accepted)
                if len(accepted) != len(data):
                    truncated = True
                if accepted:
                    text = accepted.decode("utf-8", errors="replace")
                    parts.append(text)
                    if on_chunk is not None:
                        await on_chunk(ProcessChunk(stream, text))

        stdout_task = asyncio.create_task(drain(process.stdout, "stdout", stdout_parts))
        stderr_task = asyncio.create_task(drain(process.stderr, "stderr", stderr_parts))
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            waiters = {wait_task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task is not None and cancel_task in done:
                await self._terminate(process)
                raise asyncio.CancelledError
            if wait_task not in done:
                await self._terminate(process)
                raise CommandTimeoutError(f"command timed out after {timeout_seconds:g}s")
            await asyncio.gather(stdout_task, stderr_task)
            return CommandOutcome(
                wait_task.result(),
                "".join(stdout_parts),
                "".join(stderr_parts),
                truncated,
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
            for task in (stdout_task, stderr_task, wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
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


def sanitized_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the small host environment allowed across evaluator process seams."""

    values = os.environ if source is None else source
    allowed = {
        "APPDATA",
        "COMSPEC",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PYTHONHOME",
        "PYTHONPATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
    }
    return {name: value for name, value in values.items() if name.upper() in allowed}


class ArtifactBundle:
    """Own one append-only evaluator run directory and its atomic manifest."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @classmethod
    def create(cls, root: Path) -> ArtifactBundle:
        resolved = root.resolve()
        resolved.mkdir(parents=True, exist_ok=False)
        bundle = cls(resolved)
        bundle.write_json(
            "manifest.json",
            {
                "version": 1,
                "status": "running",
                "stage": "configuration",
                "completed": False,
                "exit_code": None,
                "diagnostic": "run artifact ownership established",
                "artifacts": [],
            },
        )
        return bundle

    def _target(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise SWEbenchConfigurationError("artifact path escapes the run directory") from exc
        return target

    def write_text(self, relative: str, content: str) -> Path:
        target = self._target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(target)
        return target

    def write_json(self, relative: str, value: object) -> Path:
        return self.write_text(
            relative,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def append_jsonl(self, relative: str, value: object) -> Path:
        target = self._target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return target

    def finalize(
        self,
        *,
        status: SWEbenchStatus,
        stage: SWEbenchStage,
        exit_code: int,
        diagnostic: str,
    ) -> None:
        artifacts: list[str] = []
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if (
                not path.is_file()
                or path.name == "manifest.json"
                or ".tmp" in path.name
                or relative.parts[0] == "workspace"
            ):
                continue
            artifacts.append(relative.as_posix())
        artifacts.sort()
        self.write_json(
            "manifest.json",
            {
                "version": 1,
                "status": status,
                "stage": stage,
                "completed": True,
                "exit_code": exit_code,
                "diagnostic": diagnostic,
                "artifacts": artifacts,
            },
        )

    def record_cleanup_failure(
        self,
        *,
        operation: str,
        stage: SWEbenchStage,
        exc: BaseException,
    ) -> str:
        """Audit a cleanup error without replacing an already finalized outcome."""

        diagnostic = f"{type(exc).__name__}: {exc}"
        try:
            self.append_jsonl(
                "cleanup_failures.jsonl",
                {
                    "operation": operation,
                    "stage": stage,
                    "error_type": type(exc).__name__,
                    "diagnostic": str(exc),
                },
            )
            manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("completed") is True:
                self.finalize(
                    status=cast(SWEbenchStatus, manifest["status"]),
                    stage=cast(SWEbenchStage, manifest["stage"]),
                    exit_code=cast(int, manifest["exit_code"]),
                    diagnostic=cast(str, manifest["diagnostic"]),
                )
        except (Exception, asyncio.CancelledError):
            pass
        return f"{operation} failed: {diagnostic}"


def default_artifacts_path(instance_id: str) -> Path:
    state_root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    base = Path(state_root) if state_root else Path.home() / ".local" / "state"
    return base / "coding-agent-kernel" / "swebench" / f"{instance_id}-{uuid.uuid4().hex}"


async def check_docker_daemon(runner: CommandRunner) -> str | None:
    """Return an actionable diagnostic when Docker cannot host the evaluator."""

    environment = sanitized_subprocess_environment()
    try:
        client = await runner.run(
            ("docker", "--version"),
            cwd=None,
            env=environment,
            timeout_seconds=15.0,
            cancel_event=None,
            on_chunk=None,
            output_limit_bytes=64 * 1024,
        )
    except (OSError, RuntimeError, TimeoutError):
        return "Docker CLI is unavailable. Install Docker Desktop before running SWE-bench."
    if client.exit_code != 0:
        return "Docker CLI is unavailable. Install Docker Desktop before running SWE-bench."
    try:
        daemon = await runner.run(
            ("docker", "info", "--format", "{{json .ServerVersion}}"),
            cwd=None,
            env=environment,
            timeout_seconds=15.0,
            cancel_event=None,
            on_chunk=None,
            output_limit_bytes=64 * 1024,
        )
    except (OSError, RuntimeError, TimeoutError):
        daemon = CommandOutcome(1, "", "", False)
    if daemon.exit_code != 0:
        return (
            "Docker CLI is available, but the Linux daemon is unavailable. "
            "Start Docker Desktop manually and select Linux containers."
        )
    return None


class ContainerCodingEnvironment(LocalCodingEnvironment):
    """Evaluator-owned bind workspace with commands executed in one Docker container."""

    def __init__(
        self,
        workspace: Path,
        *,
        container_name: str,
        command_runner: CommandRunner,
        container_workdir: str = "/testbed",
    ) -> None:
        super().__init__(workspace)
        self.container_name = container_name
        self._command_runner = command_runner
        self._container_workdir = container_workdir.rstrip("/") or "/"

    def resolve_path(self, path: str) -> Path:
        """Keep adapter containment even when Kernel approval is bypassed in full mode."""

        if "\0" in path:
            raise WorkspacePathError("path target contains NUL")
        candidate = (self.workspace / path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise WorkspacePathError(f"path escapes workspace: {path}")
        return candidate

    async def write_text(
        self,
        path: str,
        content: str,
        cancel_event: asyncio.Event | None = None,
    ) -> int:
        """Write exact UTF-8 bytes so a Windows Host cannot inject CRLF into Linux files."""

        # LocalCodingEnvironment deliberately uses host-native text semantics. This
        # Adapter cannot delegate there because its Linux workspace contract requires
        # exact bytes; the cancellation test locks the shared atomic-write invariants.
        raise_if_cancelled(cancel_event)
        target = self.resolve_path(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        raise_if_cancelled(cancel_event)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        encoded = content.encode("utf-8")
        write_task = asyncio.create_task(asyncio.to_thread(temporary.write_bytes, encoded))
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
        return len(encoded)

    async def run_command(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
        cancel_event: asyncio.Event | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> ProcessResult:
        host_cwd = self.resolve_path(cwd)
        relative = host_cwd.relative_to(self.workspace).as_posix()
        container_cwd = self._container_workdir
        if relative != ".":
            container_cwd = f"{container_cwd}/{relative}"
        outcome = await self._command_runner.run(
            (
                "docker",
                "exec",
                "--workdir",
                container_cwd,
                self.container_name,
                "/bin/bash",
                "-lc",
                command,
            ),
            cwd=None,
            env=sanitized_subprocess_environment(),
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
            on_chunk=on_chunk,
            output_limit_bytes=1024 * 1024,
        )
        chunks: list[ProcessChunk] = []
        if outcome.stdout:
            chunks.append(ProcessChunk("stdout", outcome.stdout))
        if outcome.stderr:
            chunks.append(ProcessChunk("stderr", outcome.stderr))
        return ProcessResult(
            outcome.exit_code,
            outcome.stdout,
            outcome.stderr,
            tuple(chunks),
        )


@dataclass(frozen=True, slots=True)
class SWEbenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    image: str
    container_workdir: str
    container_user: str
    dataset_revision: str
    harness_version: str
    dataset_record: Mapping[str, object]


class InstanceLoader(Protocol):
    async def load(self, instance_id: str) -> SWEbenchInstance: ...


@contextmanager
def _without_host_secrets() -> Any:
    sensitive = {
        name: value
        for name, value in os.environ.items()
        if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "AUTH", "PASSWORD"))
    }
    try:
        for name in sensitive:
            os.environ.pop(name, None)
        yield
    finally:
        os.environ.update(sensitive)


@contextmanager
def _preserving_event_loop_policy() -> Any:
    """Serialize and contain process-global policy changes made by optional imports."""

    with _OFFICIAL_RUNTIME_IMPORT_LOCK:
        original = asyncio.get_event_loop_policy()
        try:
            yield
        finally:
            if asyncio.get_event_loop_policy() is not original:
                asyncio.set_event_loop_policy(original)


class OfficialInstanceLoader:
    """Load one pinned Verified datum and derive its official TestSpec image."""

    async def load(self, instance_id: str) -> SWEbenchInstance:
        return await asyncio.to_thread(self._load_sync, instance_id)

    @staticmethod
    def _load_sync(instance_id: str) -> SWEbenchInstance:
        with _preserving_event_loop_policy():
            try:
                datasets = importlib.import_module("datasets")
                test_spec_module = importlib.import_module("swebench.harness.utils")
                image_constants = importlib.import_module("swebench.image_builder.constants")
                harness_version = importlib.metadata.version("swebench")
            except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
                raise RuntimeError(
                    "official SWE-bench runtime is unavailable; install the project with "
                    "the 'swebench' optional dependency"
                ) from exc
        if harness_version != SWE_BENCH_HARNESS_VERSION:
            raise RuntimeError(
                f"unsupported SWE-bench version {harness_version}; "
                f"install exactly {SWE_BENCH_HARNESS_VERSION}"
            )
        with _without_host_secrets():
            dataset = datasets.load_dataset(
                SWE_BENCH_DATASET,
                split=SWE_BENCH_SPLIT,
                revision=SWE_BENCH_DATASET_REVISION,
            )
        matches = [item for item in dataset if item.get("instance_id") == instance_id]
        if len(matches) != 1:
            raise RuntimeError(
                f"instance {instance_id!r} was not found exactly once in SWE-bench Verified"
            )
        datum = dict(matches[0])
        required = ("instance_id", "repo", "base_commit", "problem_statement")
        if any(not isinstance(datum.get(name), str) or not datum[name] for name in required):
            raise RuntimeError("official Verified instance metadata is incomplete")
        spec = test_spec_module.make_test_spec(datum)
        image = getattr(spec, "image", None)
        workdir = getattr(image_constants, "CONTAINER_WORKDIR", None)
        user = getattr(image_constants, "CONTAINER_USER", None)
        if not all(isinstance(value, str) and value for value in (image, workdir, user)):
            raise RuntimeError("installed SWE-bench TestSpec image contract is unsupported")
        return SWEbenchInstance(
            instance_id=datum["instance_id"],
            repo=datum["repo"],
            base_commit=datum["base_commit"],
            problem_statement=datum["problem_statement"],
            image=cast(str, image),
            container_workdir=cast(str, workdir),
            container_user=cast(str, user),
            dataset_revision=SWE_BENCH_DATASET_REVISION,
            harness_version=harness_version,
            dataset_record=datum,
        )


class PreparedWorkspace(Protocol):
    @property
    def workspace(self) -> Path: ...

    @property
    def environment(self) -> LocalCodingEnvironment: ...

    async def collect_patch(self) -> str: ...

    async def close(self) -> None: ...


class WorkspaceFactory(Protocol):
    async def prepare(
        self,
        instance: SWEbenchInstance,
        artifacts: ArtifactBundle,
    ) -> PreparedWorkspace: ...


@dataclass(frozen=True, slots=True)
class SWEbenchRunConfig:
    instance_id: str
    model: str
    mode: PermissionMode
    agent_timeout_seconds: float
    harness_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HarnessEvaluation:
    status: SWEbenchStatus
    stage: SWEbenchStage
    exit_code: int
    diagnostic: str
    resolved: bool | None


class HarnessRunner(Protocol):
    async def evaluate(
        self,
        *,
        config: SWEbenchRunConfig,
        instance: SWEbenchInstance,
        prediction_path: Path,
        artifacts: ArtifactBundle,
        run_id: str,
    ) -> HarnessEvaluation: ...


@dataclass(frozen=True, slots=True)
class SWEbenchDependencies:
    instance_loader: InstanceLoader
    workspace_factory: WorkspaceFactory
    harness_runner: HarnessRunner


@dataclass(frozen=True, slots=True)
class SWEbenchExecution:
    status: SWEbenchStatus
    stage: SWEbenchStage
    exit_code: int
    diagnostic: str
    artifacts: Path


PermissionResolver = Callable[[str], Awaitable[bool]]
StatusCallback = Callable[[SWEbenchStage, SWEbenchStatus, str], None]


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return repr(value)


def validate_prediction_patch(patch: str) -> str:
    if not patch.strip():
        raise SWEbenchConfigurationError("workspace produced no git patch")
    if "\0" in patch or len(patch.encode("utf-8")) > _MAX_PREDICTION_PATCH_BYTES:
        raise SWEbenchConfigurationError("workspace patch is invalid or exceeds 10 MiB")
    headers = [line for line in patch.splitlines() if line.startswith("diff --git ")]
    if not headers:
        raise SWEbenchConfigurationError("workspace patch is not a git diff")
    for header in headers:
        try:
            fields = shlex.split(header)
        except ValueError as exc:
            raise SWEbenchConfigurationError("workspace patch has an invalid diff header") from exc
        if len(fields) != 4:
            raise SWEbenchConfigurationError("workspace patch has an invalid diff header")
        for prefix, raw in (("a/", fields[2]), ("b/", fields[3])):
            if not raw.startswith(prefix):
                raise SWEbenchConfigurationError("workspace patch has a non-relative diff path")
            relative = Path(raw[2:])
            if relative.is_absolute() or ".." in relative.parts:
                raise SWEbenchConfigurationError("workspace patch path escapes the repository")
    return patch if patch.endswith("\n") else patch + "\n"


async def _checked_command(
    runner: CommandRunner,
    argv: tuple[str, ...],
    *,
    operation: str | None = None,
    cwd: Path | None = None,
    timeout_seconds: float = 120.0,
    output_limit_bytes: int = 4 * 1024 * 1024,
) -> CommandOutcome:
    outcome = await runner.run(
        argv,
        cwd=cwd,
        env=sanitized_subprocess_environment(),
        timeout_seconds=timeout_seconds,
        cancel_event=None,
        on_chunk=None,
        output_limit_bytes=output_limit_bytes,
    )
    label = operation or f"command {argv[0]!r}"
    if outcome.exit_code != 0:
        detail = outcome.stderr or outcome.stdout
        raise RuntimeError(
            f"{label} failed with exit status {outcome.exit_code}: {_diagnostic_excerpt(detail)}"
        )
    if outcome.output_truncated:
        raise SWEbenchConfigurationError(f"{label} output was truncated")
    return outcome


_SECRET_DIAGNOSTIC = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|AUTH|PASSWORD)[A-Z0-9_]*)\s*[:=]\s*\S+"
)
_RAW_DIFF_METADATA = re.compile(r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) (M)")


def _diagnostic_excerpt(value: str, *, limit: int = 2048) -> str:
    redacted = _SECRET_DIAGNOSTIC.sub(r"\1=<redacted>", value.strip())
    flattened = " | ".join(line.strip() for line in redacted.splitlines() if line.strip())
    if len(flattened) <= limit:
        return flattened or "no command output"
    return flattened[:limit] + "... [diagnostic truncated]"


def _validate_mode_only_image_delta(raw_delta: str) -> None:
    for record in raw_delta.splitlines():
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        match = _RAW_DIFF_METADATA.fullmatch(metadata)
        if separator != "\t" or not path or match is None:
            raise RuntimeError(
                "official image commit delta could not be validated as mode-only: "
                f"{_diagnostic_excerpt(record)}"
            )
        old_mode, new_mode, old_blob, new_blob, _ = match.groups()
        if (
            old_blob != new_blob
            or old_mode == new_mode
            or {old_mode, new_mode} != {"100644", "100755"}
        ):
            raise RuntimeError(
                "official image HEAD changes repository content or type relative to the "
                f"instance base commit: {_diagnostic_excerpt(record)}"
            )


def _validate_tracked_symlinks(workspace: Path, staged_files: str) -> None:
    for record in staged_files.split("\0"):
        if not record:
            continue
        metadata, separator, relative = record.partition("\t")
        fields = metadata.split()
        if separator != "\t" or len(fields) != 3 or fields[2] != "0":
            raise RuntimeError("normalized workspace Git index contains an invalid entry")
        mode = fields[0]
        if mode != "120000":
            continue
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\\" in relative:
            raise RuntimeError(f"tracked symlink path is unsafe: {_diagnostic_excerpt(relative)}")
        link = workspace.joinpath(*relative_path.parts)
        if not link.is_symlink():
            raise RuntimeError(
                f"tracked symlink was not materialized safely: {_diagnostic_excerpt(relative)}"
            )
        target = os.readlink(link)
        target_path = Path(target)
        if target_path.is_absolute() or target_path.drive:
            raise RuntimeError(
                f"tracked symlink escapes workspace: {_diagnostic_excerpt(relative)}"
            )
        try:
            resolved_target = (link.parent / target_path).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"tracked symlink cannot be resolved safely: {_diagnostic_excerpt(relative)}"
            ) from exc
        if resolved_target != workspace and workspace not in resolved_target.parents:
            raise RuntimeError(
                f"tracked symlink escapes workspace: {_diagnostic_excerpt(relative)}"
            )


def _resolve_git_path(value: str, *, workspace: Path) -> Path:
    path = Path(value.strip())
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def _validate_owned_git_directory(workspace: Path) -> None:
    git_directory = workspace / ".git"
    for root, directories, files in os.walk(git_directory, followlinks=False):
        for name in (*directories, *files):
            entry = Path(root) / name
            if entry.is_symlink():
                relative = entry.relative_to(git_directory).as_posix()
                raise RuntimeError(
                    "copied workspace Git directory contains a symlink: "
                    f"{_diagnostic_excerpt(relative)}"
                )


class DockerPreparedWorkspace:
    def __init__(
        self,
        workspace: Path,
        *,
        container_name: str,
        command_runner: CommandRunner,
        container_workdir: str,
        base_commit: str,
    ) -> None:
        self.workspace = workspace.resolve()
        self._container_name = container_name
        self._command_runner = command_runner
        self._base_commit = base_commit
        self._closed = False
        self.environment = ContainerCodingEnvironment(
            self.workspace,
            container_name=container_name,
            command_runner=command_runner,
            container_workdir=container_workdir,
        )

    async def collect_patch(self) -> str:
        await _checked_command(
            self._command_runner,
            ("git", "add", "-N", "--all"),
            cwd=self.workspace,
        )
        check = await self._command_runner.run(
            ("git", "diff", "--check", self._base_commit, "--", "."),
            cwd=self.workspace,
            env=sanitized_subprocess_environment(),
            timeout_seconds=60.0,
            cancel_event=None,
            on_chunk=None,
            output_limit_bytes=1024 * 1024,
        )
        if check.exit_code != 0:
            raise SWEbenchConfigurationError("workspace git diff failed whitespace validation")
        diff = await _checked_command(
            self._command_runner,
            (
                "git",
                "diff",
                "--binary",
                "--no-ext-diff",
                self._base_commit,
                "--",
                ".",
            ),
            cwd=self.workspace,
            output_limit_bytes=_MAX_PREDICTION_PATCH_BYTES + 1,
        )
        return diff.stdout

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._command_runner.run(
            ("docker", "rm", "--force", self._container_name),
            cwd=None,
            env=sanitized_subprocess_environment(),
            timeout_seconds=60.0,
            cancel_event=None,
            on_chunk=None,
            output_limit_bytes=1024 * 1024,
        )


class DockerWorkspaceFactory:
    """Copy a preloaded official image workspace and bind it to an owned agent container."""

    def __init__(self, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

    async def prepare(
        self,
        instance: SWEbenchInstance,
        artifacts: ArtifactBundle,
    ) -> PreparedWorkspace:
        workspace = (artifacts.root / "workspace").resolve()
        if "," in str(workspace):
            raise SWEbenchConfigurationError("artifact workspace path cannot contain a comma")
        if workspace.parent != artifacts.root or workspace.exists():
            raise SWEbenchConfigurationError(
                "artifact workspace must be a new evaluator-owned directory"
            )
        image = await self._command_runner.run(
            ("docker", "image", "inspect", instance.image),
            cwd=None,
            env=sanitized_subprocess_environment(),
            timeout_seconds=30.0,
            cancel_event=None,
            on_chunk=None,
            output_limit_bytes=1024 * 1024,
        )
        if image.exit_code != 0:
            raise RuntimeError(
                "official instance image is not preloaded; prepare it explicitly with the "
                "official SWE-bench tooling before this command"
            )
        suffix = hashlib.sha256(f"{instance.instance_id}:{artifacts.root}".encode()).hexdigest()[
            :16
        ]
        seed_name = f"coding-agent-sweb-seed-{suffix}"
        agent_name = f"coding-agent-sweb-agent-{suffix}"
        seed_created = False
        try:
            await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "create",
                    "--name",
                    seed_name,
                    "--user",
                    instance.container_user,
                    "--workdir",
                    instance.container_workdir,
                    instance.image,
                    "tail",
                    "-f",
                    "/dev/null",
                ),
                operation="creating the official image inspection container",
            )
            seed_created = True
            await _checked_command(
                self._command_runner,
                ("docker", "start", seed_name),
                operation="starting the official image inspection container",
            )
            seed_head = await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "exec",
                    "--workdir",
                    instance.container_workdir,
                    seed_name,
                    "git",
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ),
                operation="reading the official image workspace HEAD",
            )
            image_head = seed_head.stdout.strip()
            image_status = await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "exec",
                    "--workdir",
                    instance.container_workdir,
                    seed_name,
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
                operation="checking the official image workspace status",
            )
            if image_status.stdout:
                raise RuntimeError(
                    "official image workspace is dirty; refusing to normalize it: "
                    f"{_diagnostic_excerpt(image_status.stdout)}"
                )
            await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "exec",
                    "--workdir",
                    instance.container_workdir,
                    seed_name,
                    "git",
                    "cat-file",
                    "-e",
                    f"{instance.base_commit}^{{commit}}",
                ),
                operation="verifying the instance base commit exists in the official image",
            )
            await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "exec",
                    "--workdir",
                    instance.container_workdir,
                    seed_name,
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    instance.base_commit,
                    image_head,
                ),
                operation="verifying the instance base commit is an ancestor of image HEAD",
            )
            image_delta = await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "exec",
                    "--workdir",
                    instance.container_workdir,
                    seed_name,
                    "git",
                    "diff",
                    "--raw",
                    "--no-abbrev",
                    "--no-renames",
                    instance.base_commit,
                    image_head,
                    "--",
                    ".",
                ),
                operation="validating the official image commit delta",
                output_limit_bytes=4 * 1024 * 1024,
            )
            _validate_mode_only_image_delta(image_delta.stdout)
            await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "cp",
                    f"{seed_name}:{instance.container_workdir}/.",
                    str(workspace),
                ),
                operation="copying the official image workspace to the evaluator artifact",
            )
        finally:
            if seed_created:
                await self._command_runner.run(
                    ("docker", "rm", "--force", seed_name),
                    cwd=None,
                    env=sanitized_subprocess_environment(),
                    timeout_seconds=60.0,
                    cancel_event=None,
                    on_chunk=None,
                    output_limit_bytes=1024 * 1024,
                )
        if not workspace.is_dir():
            raise RuntimeError("official instance image did not provide the target workspace")
        if (workspace / ".git").is_symlink() or not (workspace / ".git").is_dir():
            raise RuntimeError("copied workspace does not contain an owned Git directory")
        _validate_owned_git_directory(workspace)
        top_level = await _checked_command(
            self._command_runner,
            ("git", "rev-parse", "--show-toplevel"),
            cwd=workspace,
            operation="verifying the copied workspace Git root",
        )
        if _resolve_git_path(top_level.stdout, workspace=workspace) != workspace:
            raise RuntimeError("copied workspace Git root escapes the evaluator-owned workspace")
        common_dir = await _checked_command(
            self._command_runner,
            ("git", "rev-parse", "--git-common-dir"),
            cwd=workspace,
            operation="verifying the copied workspace Git object directory",
        )
        if _resolve_git_path(common_dir.stdout, workspace=workspace) != _resolve_git_path(
            ".git", workspace=workspace
        ):
            raise RuntimeError("copied workspace Git object directory is not evaluator-owned")
        copied_head = await _checked_command(
            self._command_runner,
            ("git", "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=workspace,
            operation="verifying the copied workspace HEAD",
        )
        if copied_head.stdout.strip() != image_head:
            raise RuntimeError(
                "copied workspace HEAD differs from the validated official image HEAD"
            )
        await _checked_command(
            self._command_runner,
            ("git", "config", "--local", "core.filemode", "false"),
            cwd=workspace,
            operation="configuring cross-platform Git file mode handling",
        )
        await _checked_command(
            self._command_runner,
            ("git", "config", "--local", "core.autocrlf", "false"),
            cwd=workspace,
            operation="configuring cross-platform Git line ending handling",
        )
        await _checked_command(
            self._command_runner,
            ("git", "config", "--local", "core.symlinks", "true"),
            cwd=workspace,
            operation="configuring tracked symlink checkout",
        )
        await _checked_command(
            self._command_runner,
            ("git", "reset", "--hard", instance.base_commit),
            cwd=workspace,
            operation="normalizing the evaluator-owned workspace to the instance base commit",
        )
        normalized_head = await _checked_command(
            self._command_runner,
            ("git", "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=workspace,
            operation="verifying the normalized workspace HEAD",
        )
        if normalized_head.stdout.strip() != instance.base_commit:
            raise RuntimeError("normalized workspace HEAD does not match the instance base commit")
        clean = await _checked_command(
            self._command_runner,
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=workspace,
            operation="verifying the normalized workspace status",
        )
        if clean.stdout:
            raise RuntimeError(
                "normalized workspace is not clean at the instance base commit: "
                f"{_diagnostic_excerpt(clean.stdout)}"
            )
        staged_files = await _checked_command(
            self._command_runner,
            ("git", "ls-files", "--stage", "-z"),
            cwd=workspace,
            operation="enumerating tracked symlinks in the normalized workspace",
            output_limit_bytes=4 * 1024 * 1024,
        )
        _validate_tracked_symlinks(workspace, staged_files.stdout)
        mount = f"type=bind,source={workspace},target={instance.container_workdir}"
        try:
            await _checked_command(
                self._command_runner,
                (
                    "docker",
                    "create",
                    "--name",
                    agent_name,
                    "--network",
                    "none",
                    "--user",
                    instance.container_user,
                    "--workdir",
                    instance.container_workdir,
                    "--mount",
                    mount,
                    instance.image,
                    "tail",
                    "-f",
                    "/dev/null",
                ),
                operation="creating the controlled agent container",
            )
            await _checked_command(
                self._command_runner,
                ("docker", "start", agent_name),
                operation="starting the controlled agent container",
            )
        except BaseException:
            await self._command_runner.run(
                ("docker", "rm", "--force", agent_name),
                cwd=None,
                env=sanitized_subprocess_environment(),
                timeout_seconds=60.0,
                cancel_event=None,
                on_chunk=None,
                output_limit_bytes=1024 * 1024,
            )
            raise
        return DockerPreparedWorkspace(
            workspace,
            container_name=agent_name,
            command_runner=self._command_runner,
            container_workdir=instance.container_workdir,
            base_commit=instance.base_commit,
        )


class OfficialHarnessRunner:
    """Invoke and ingest the official Harness contract without wrapping verdicts."""

    def __init__(self, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

    async def evaluate(
        self,
        *,
        config: SWEbenchRunConfig,
        instance: SWEbenchInstance,
        prediction_path: Path,
        artifacts: ArtifactBundle,
        run_id: str,
    ) -> HarnessEvaluation:
        harness_root = artifacts.root / "harness"
        harness_root.mkdir(parents=True, exist_ok=False)
        dataset_path = artifacts.write_json(
            "official_instance.json", [dict(instance.dataset_record)]
        )
        harness_entrypoint = (
            (sys.executable, "-c", _WINDOWS_HARNESS_BOOTSTRAP)
            if os.name == "nt"
            else (sys.executable, "-m", "swebench.harness.run_evaluation")
        )
        argv = (
            *harness_entrypoint,
            "--dataset_name",
            str(dataset_path),
            "--split",
            SWE_BENCH_SPLIT,
            "--predictions_path",
            str(prediction_path),
            "--max_workers",
            "1",
            "--run_id",
            run_id,
            "--timeout",
            str(max(1, int(config.harness_timeout_seconds))),
            "--instance_ids",
            instance.instance_id,
        )
        process_environment = sanitized_subprocess_environment()
        artifacts.write_json(
            "harness_argv.json",
            {
                "argv": list(argv),
                "cwd": "harness",
                "environment_names": sorted(process_environment),
                "dataset": SWE_BENCH_DATASET,
                "dataset_revision": instance.dataset_revision,
            },
        )
        try:
            outcome = await self._command_runner.run(
                argv,
                cwd=harness_root,
                env=process_environment,
                timeout_seconds=config.harness_timeout_seconds + 300.0,
                cancel_event=None,
                on_chunk=None,
                output_limit_bytes=4 * 1024 * 1024,
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            return HarnessEvaluation(
                "harness_invocation_failed",
                "harness",
                7,
                f"official Harness could not be invoked: {type(exc).__name__}: {exc}",
                None,
            )
        artifacts.write_text("harness_stdout.log", outcome.stdout)
        artifacts.write_text("harness_stderr.log", outcome.stderr)
        artifacts.write_json(
            "harness_process.json",
            {
                "exit_code": outcome.exit_code,
                "output_truncated": outcome.output_truncated,
            },
        )
        if outcome.exit_code != 0:
            return HarnessEvaluation(
                "harness_invocation_failed",
                "harness",
                7,
                f"official Harness exited with status {outcome.exit_code}",
                None,
            )
        model_dir = config.model.replace("/", "__")
        summary_path = harness_root / f"{model_dir}.{run_id}.json"
        report_path = (
            harness_root
            / "logs"
            / "run_evaluation"
            / run_id
            / model_dir
            / instance.instance_id
            / "report.json"
        )
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            resolved = report[instance.instance_id]["resolved"]
            if type(resolved) is not bool:
                raise ValueError("resolved must be a boolean")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return HarnessEvaluation(
                "harness_rejected",
                "harness_result",
                7,
                f"official Harness result is missing or invalid: {type(exc).__name__}: {exc}",
                None,
            )
        artifacts.write_json("harness_summary.json", summary)
        artifacts.write_json("harness_instance_result.json", report)
        identifier_fields = (
            "submitted_ids",
            "completed_ids",
            "resolved_ids",
            "unresolved_ids",
            "error_ids",
            "empty_patch_ids",
            "incomplete_ids",
            "infra_failure_ids",
            "ambiguous_failure_ids",
        )
        if any(not isinstance(summary.get(name), list) for name in identifier_fields):
            return HarnessEvaluation(
                "harness_rejected",
                "harness_result",
                7,
                "official Harness summary identifier fields are missing or invalid",
                None,
            )
        membership = {name: instance.instance_id in summary[name] for name in identifier_fields}
        for field, description in (
            ("infra_failure_ids", "infrastructure failure"),
            ("ambiguous_failure_ids", "ambiguous infrastructure failure"),
        ):
            if membership[field]:
                return HarnessEvaluation(
                    "harness_rejected",
                    "harness_result",
                    7,
                    f"official Harness reported an {description} for the instance",
                    None,
                )
        lifecycle_valid = (
            membership["submitted_ids"]
            and membership["completed_ids"]
            and not membership["error_ids"]
            and not membership["empty_patch_ids"]
            and not membership["incomplete_ids"]
        )
        if (
            resolved
            and lifecycle_valid
            and membership["resolved_ids"]
            and not membership["unresolved_ids"]
        ):
            return HarnessEvaluation(
                "success",
                "complete",
                0,
                "official Harness resolved the instance",
                True,
            )
        if (
            not resolved
            and lifecycle_valid
            and membership["unresolved_ids"]
            and not membership["resolved_ids"]
        ):
            return HarnessEvaluation(
                "harness_failed",
                "harness_result",
                7,
                "official Harness completed and reported the instance unresolved",
                False,
            )
        return HarnessEvaluation(
            "harness_rejected",
            "harness_result",
            7,
            "official Harness summary and instance report disagree",
            None,
        )


class SWEbenchEvaluator:
    """Coordinate one Kernel run, one prediction, and one official verdict."""

    def __init__(self, provider: ModelProvider, dependencies: SWEbenchDependencies) -> None:
        self._provider = provider
        self._dependencies = dependencies

    async def run(
        self,
        config: SWEbenchRunConfig,
        artifacts: ArtifactBundle,
        *,
        permission_resolver: PermissionResolver,
        on_status: StatusCallback | None = None,
    ) -> SWEbenchExecution:
        prepared: PreparedWorkspace | None = None
        kernel: AgentKernel | None = None
        agent_run: AgentRun | None = None
        consumer: asyncio.Task[AgentRunResult] | None = None
        current_stage: SWEbenchStage = "instance_loading"

        def status(stage: SWEbenchStage, value: SWEbenchStatus, diagnostic: str) -> None:
            if on_status is not None:
                on_status(stage, value, diagnostic)

        def failure(
            value: SWEbenchStatus,
            stage: SWEbenchStage,
            exit_code: int,
            diagnostic: str,
        ) -> SWEbenchExecution:
            artifacts.finalize(
                status=value,
                stage=stage,
                exit_code=exit_code,
                diagnostic=diagnostic,
            )
            return SWEbenchExecution(value, stage, exit_code, diagnostic, artifacts.root)

        async def stop_agent() -> None:
            nonlocal kernel
            cancel_failed = False
            if agent_run is not None:
                try:
                    await asyncio.wait_for(agent_run.cancel(), timeout=_CLEANUP_TIMEOUT_SECONDS)
                except (Exception, asyncio.CancelledError) as exc:
                    cancel_failed = True
                    artifacts.record_cleanup_failure(
                        operation="agent_run.cancel", stage=current_stage, exc=exc
                    )
            if consumer is not None:
                if cancel_failed and not consumer.done():
                    consumer.cancel()
                try:
                    done, pending = await asyncio.wait({consumer}, timeout=_CLEANUP_TIMEOUT_SECONDS)
                except (Exception, asyncio.CancelledError) as exc:
                    artifacts.record_cleanup_failure(
                        operation="agent_run.consumer", stage=current_stage, exc=exc
                    )
                    consumer.cancel()
                else:
                    if pending:
                        consumer.cancel()
                        artifacts.record_cleanup_failure(
                            operation="agent_run.consumer",
                            stage=current_stage,
                            exc=TimeoutError(
                                f"consumer cleanup exceeded {_CLEANUP_TIMEOUT_SECONDS:g} seconds"
                            ),
                        )
                        try:
                            done_after_cancel, pending = await asyncio.wait(
                                pending, timeout=_CLEANUP_TIMEOUT_SECONDS
                            )
                            done.update(done_after_cancel)
                        except (Exception, asyncio.CancelledError) as exc:
                            artifacts.record_cleanup_failure(
                                operation="agent_run.consumer",
                                stage=current_stage,
                                exc=exc,
                            )
                    for task in done:
                        if not task.cancelled():
                            task.exception()
            if kernel is not None:
                try:
                    kernel.close_session()
                except (Exception, asyncio.CancelledError) as exc:
                    artifacts.record_cleanup_failure(
                        operation="kernel.close_session", stage=current_stage, exc=exc
                    )
                finally:
                    kernel = None

        async def close_workspace(*, preserve_terminal_outcome: bool) -> str | None:
            nonlocal prepared
            workspace = prepared
            prepared = None
            if workspace is None:
                return None
            try:
                await workspace.close()
            except asyncio.CancelledError as exc:
                if not preserve_terminal_outcome:
                    raise
                return artifacts.record_cleanup_failure(
                    operation="workspace.close", stage=current_stage, exc=exc
                )
            except Exception as exc:
                return artifacts.record_cleanup_failure(
                    operation="workspace.close", stage=current_stage, exc=exc
                )
            return None

        try:
            status("instance_loading", "running", "loading official Verified instance metadata")
            instance = await self._dependencies.instance_loader.load(config.instance_id)
            if instance.instance_id != config.instance_id:
                return failure(
                    "environment_preparation_failed",
                    "instance_loading",
                    3,
                    "instance loader returned metadata for a different instance",
                )
            artifacts.write_json(
                "provenance.json",
                {
                    "version": 1,
                    "instance_id": instance.instance_id,
                    "repo": instance.repo,
                    "base_commit": instance.base_commit,
                    "image": instance.image,
                    "dataset": SWE_BENCH_DATASET,
                    "split": SWE_BENCH_SPLIT,
                    "dataset_revision": instance.dataset_revision,
                    "harness_version": instance.harness_version,
                    "official_contract_commit": SWE_BENCH_CONTRACT_COMMIT,
                },
            )
            current_stage = "environment_preparation"
            status(current_stage, "running", "preparing evaluator-owned workspace")
            prepared = await self._dependencies.workspace_factory.prepare(instance, artifacts)
            artifacts.write_text("events.jsonl", "")
            artifacts.write_text("tool_results.jsonl", "")
            runtime = ToolRuntime(prepared.environment)
            runtime.enable("grep", "find", "ls")
            store = JsonlSessionStore(artifacts.root / "session.jsonl")
            context_settings = ContextSettings(
                system_prompt=(
                    "You are a headless coding agent. Work directly on the requested task in "
                    "the provided workspace. Inspect only what is needed, make the necessary "
                    "edits, and run focused tests. Once the task is complete, finish with a "
                    "concise response without calling another tool."
                ),
                tool_guidelines=(
                    "Use only the active tools described in this request. Prefer focused file "
                    "inspection and targeted tests. Avoid repeating equivalent inspection "
                    "commands after their result is already known."
                ),
                project_context=(f"Workspace root: {instance.container_workdir}",),
            )
            try:
                kernel_distribution_version = importlib.metadata.version("coding-agent-kernel")
            except importlib.metadata.PackageNotFoundError:
                kernel_distribution_version = "source-tree"
            kernel_configuration = {
                "provider": "deepseek",
                "model": config.model,
                "workspace": instance.container_workdir,
                "instance_id": instance.instance_id,
                "permission_mode": config.mode.value,
                "max_turns": None,
                "enabled_tools": ["read", "write", "edit", "bash", "grep", "find", "ls"],
                "context_settings": _jsonable(context_settings),
                "kernel_distribution_version": kernel_distribution_version,
            }
            artifacts.write_json("kernel_configuration.json", kernel_configuration)
            kernel = AgentKernel.with_new_session(
                self._provider,
                store,
                configuration=kernel_configuration,
                session_id=f"swebench-{uuid.uuid4().hex}",
                tool_runtime=runtime,
                context_settings=context_settings,
            )
            current_stage = "agent"
            status(
                current_stage,
                "running",
                "AgentKernel is processing the Verified problem statement",
            )
            run = kernel.create_run(
                instance.problem_statement,
                permission_mode=config.mode,
                max_turns=None,
            )
            agent_run = run

            async def consume() -> AgentRunResult:
                async for event in run:
                    artifacts.append_jsonl("events.jsonl", _jsonable(event))
                    if isinstance(event, AgentSessionEvent) and event.tool_result is not None:
                        artifacts.append_jsonl("tool_results.jsonl", _jsonable(event.tool_result))
                    if event.permission_request is not None:
                        approved = await permission_resolver(event.permission_request.request_id)
                        await run.resolve_permission(event.permission_request.request_id, approved)
                return await run.result()

            consumer = asyncio.create_task(consume())
            done, _ = await asyncio.wait({consumer}, timeout=config.agent_timeout_seconds)
            if not done:
                await stop_agent()
                return failure(
                    "timed_out",
                    "agent",
                    5,
                    f"Agent Run exceeded {config.agent_timeout_seconds:g} seconds",
                )
            result = consumer.result()
            kernel.close_session()
            kernel = None
            if result.state is AgentRunState.CANCELLED:
                return failure("cancelled", "agent", 5, "Agent Run was cancelled")
            if result.state is not AgentRunState.SETTLED:
                diagnostic = (
                    "Agent Run failed"
                    if result.error is None
                    else f"Agent Run failed: {result.error.code}: {result.error.message}"
                )
                return failure("agent_failed", "agent", 4, diagnostic)
            current_stage = "prediction"
            status(current_stage, "running", "serializing workspace git diff")
            raw_patch = await prepared.collect_patch()
            try:
                patch = validate_prediction_patch(raw_patch)
            except SWEbenchConfigurationError as exc:
                value: SWEbenchStatus = (
                    "no_patch" if not raw_patch.strip() else "prediction_invalid"
                )
                return failure(value, "prediction", 6, str(exc))
            artifacts.write_text("workspace.patch", patch)
            prediction_path = artifacts.write_text(
                "prediction.jsonl",
                json.dumps(
                    {
                        "instance_id": instance.instance_id,
                        "model_name_or_path": config.model,
                        "model_patch": patch,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n",
            )
            cleanup_failure = await close_workspace(preserve_terminal_outcome=False)
            if cleanup_failure is not None:
                return failure(
                    "environment_preparation_failed",
                    "environment_preparation",
                    3,
                    cleanup_failure,
                )
            patch_digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:12]
            run_id = f"coding-agent-{patch_digest}-{uuid.uuid4().hex[:12]}"
            artifacts.write_json(
                "harness_invocation.json",
                {
                    "module": "swebench.harness.run_evaluation",
                    "dataset": SWE_BENCH_DATASET,
                    "split": SWE_BENCH_SPLIT,
                    "instance_ids": [instance.instance_id],
                    "run_id": run_id,
                    "timeout": config.harness_timeout_seconds,
                    "prediction_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
                },
            )
            current_stage = "harness"
            status(current_stage, "running", "invoking the official SWE-bench Harness")
            harness = await self._dependencies.harness_runner.evaluate(
                config=config,
                instance=instance,
                prediction_path=prediction_path,
                artifacts=artifacts,
                run_id=run_id,
            )
            artifacts.finalize(
                status=harness.status,
                stage=harness.stage,
                exit_code=harness.exit_code,
                diagnostic=harness.diagnostic,
            )
            return SWEbenchExecution(
                harness.status,
                harness.stage,
                harness.exit_code,
                harness.diagnostic,
                artifacts.root,
            )
        except asyncio.CancelledError:
            await stop_agent()
            return failure("cancelled", current_stage, 5, "evaluator was cancelled")
        except Exception as exc:
            await stop_agent()
            classifications: dict[SWEbenchStage, tuple[SWEbenchStatus, int]] = {
                "instance_loading": ("environment_preparation_failed", 3),
                "environment_preparation": ("environment_preparation_failed", 3),
                "agent": ("agent_failed", 4),
                "prediction": ("prediction_invalid", 6),
                "harness": ("harness_invocation_failed", 7),
            }
            value, exit_code = classifications[current_stage]
            return failure(
                value,
                current_stage,
                exit_code,
                f"{current_stage} failed: {type(exc).__name__}: {exc}",
            )
        finally:
            await close_workspace(preserve_terminal_outcome=True)


def production_dependencies(command_runner: CommandRunner) -> SWEbenchDependencies:
    """Assemble the official dataset, Docker workspace, and Harness adapters."""

    return SWEbenchDependencies(
        instance_loader=OfficialInstanceLoader(),
        workspace_factory=DockerWorkspaceFactory(command_runner),
        harness_runner=OfficialHarnessRunner(command_runner),
    )
