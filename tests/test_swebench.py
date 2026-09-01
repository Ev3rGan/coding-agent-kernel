from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from coding_agent.cli import main
from coding_agent.environment import CommandTimeoutError, LocalCodingEnvironment, WorkspacePathError
from coding_agent.events import (
    ProviderDone,
    ProviderStreamEvent,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
)
from coding_agent.kernel import AgentKernel
from coding_agent.permissions import PermissionMode
from coding_agent.provider import FakeProvider, ProviderRequest
from coding_agent.run import AgentRun
from coding_agent.swebench import (
    SWE_BENCH_DATASET,
    SWE_BENCH_DATASET_REVISION,
    SWE_BENCH_SPLIT,
    ArtifactBundle,
    CommandOutcome,
    ContainerCodingEnvironment,
    DockerWorkspaceFactory,
    OfficialHarnessRunner,
    OfficialInstanceLoader,
    SubprocessCommandRunner,
    SWEbenchConfigurationError,
    SWEbenchDependencies,
    SWEbenchEvaluator,
    SWEbenchInstance,
    SWEbenchRunConfig,
)


def _records(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line]


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


class _ScriptedCommandRunner:
    def __init__(self, *outcomes: CommandOutcome) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def run(self, argv: tuple[str, ...], **options: Any) -> CommandOutcome:
        self.calls.append((argv, options))
        return next(self.outcomes)


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, *items: dict[str, Any] | str) -> None:
        lines = [
            f"data: {item if isinstance(item, str) else json.dumps(item)}\n\n".encode()
            for item in items
        ]
        self._content = b"".join(lines)

    async def __aiter__(self) -> Any:
        yield self._content


class _FixtureInstanceLoader:
    async def load(self, instance_id: str) -> SWEbenchInstance:
        return SWEbenchInstance(
            instance_id=instance_id,
            repo="synthetic/fixture",
            base_commit="a" * 40,
            problem_statement="Change value.txt from before to after and verify it.",
            image="swebench/sweb.eval.synthetic_fixture:latest",
            container_workdir="/testbed",
            container_user="root",
            dataset_revision="fixture-revision",
            harness_version="fixture-harness",
            dataset_record={
                "instance_id": instance_id,
                "repo": "synthetic/fixture",
                "base_commit": "a" * 40,
                "problem_statement": "Change value.txt from before to after and verify it.",
            },
        )


class _MissingMetadataInstanceLoader:
    async def load(self, instance_id: str) -> SWEbenchInstance:
        del instance_id
        raise KeyError("FAIL_TO_PASS")


class _FixturePreparedWorkspace:
    def __init__(self, root: Path) -> None:
        self.workspace = root
        self.environment = LocalCodingEnvironment(root)
        self.closed = False

    async def collect_patch(self) -> str:
        assert (self.workspace / "value.txt").read_text(encoding="utf-8") == "after\n"
        return (
            "diff --git a/value.txt b/value.txt\n"
            "index 90e5137..b6fc4c6 100644\n"
            "--- a/value.txt\n"
            "+++ b/value.txt\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )

    async def close(self) -> None:
        self.closed = True


class _FixtureWorkspaceFactory:
    def __init__(self, root: Path) -> None:
        self.prepared = _FixturePreparedWorkspace(root)

    async def prepare(self, instance: SWEbenchInstance, artifacts: Any) -> Any:
        del instance, artifacts
        return self.prepared


class _PatchPreparedWorkspace:
    def __init__(self, root: Path, patch: str) -> None:
        self.workspace = root
        self.environment = LocalCodingEnvironment(root)
        self.patch = patch
        self.closed = False

    async def collect_patch(self) -> str:
        return self.patch

    async def close(self) -> None:
        self.closed = True


class _FailingClosePreparedWorkspace(_PatchPreparedWorkspace):
    async def close(self) -> None:
        self.closed = True
        raise OSError("workspace cleanup exploded")


class _CancelledClosePreparedWorkspace(_PatchPreparedWorkspace):
    async def close(self) -> None:
        self.closed = True
        raise asyncio.CancelledError("workspace cleanup cancelled")


class _PatchWorkspaceFactory:
    def __init__(self, root: Path, patch: str) -> None:
        self.prepared = _PatchPreparedWorkspace(root, patch)

    async def prepare(self, instance: SWEbenchInstance, artifacts: Any) -> Any:
        del instance, artifacts
        return self.prepared


class _FailingCloseWorkspaceFactory(_PatchWorkspaceFactory):
    def __init__(self, root: Path, patch: str) -> None:
        self.prepared = _FailingClosePreparedWorkspace(root, patch)


class _CancelledCloseWorkspaceFactory(_PatchWorkspaceFactory):
    def __init__(self, root: Path, patch: str) -> None:
        self.prepared = _CancelledClosePreparedWorkspace(root, patch)


class _NeverHarness:
    def __init__(self) -> None:
        self.called = False

    async def evaluate(self, **options: Any) -> Any:
        del options
        self.called = True
        raise AssertionError("Harness must not run for an invalid prediction")


class _BlockingProvider:
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        del request
        await asyncio.sleep(10)
        yield ProviderDone()


class _OfficialResultCommandRunner:
    def __init__(self, mode: str = "resolved") -> None:
        self.mode = mode
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def run(self, argv: tuple[str, ...], **options: Any) -> CommandOutcome:
        self.calls.append((argv, options))
        if argv == ("docker", "--version"):
            return CommandOutcome(0, "Docker version 29.7.2\n", "", False)
        if argv[:2] == ("docker", "info"):
            return CommandOutcome(0, '"29.7.2"\n', "", False)
        assert argv[:3] == (sys.executable, "-m", "swebench.harness.run_evaluation")
        if self.mode == "invocation-failed":
            return CommandOutcome(2, "", "Harness rejected argv", False)
        if self.mode == "missing-result":
            return CommandOutcome(0, "No instances to run.\n", "", False)
        cwd = options["cwd"]
        run_id = argv[argv.index("--run_id") + 1]
        instance_id = argv[argv.index("--instance_ids") + 1]
        prediction_path = Path(argv[argv.index("--predictions_path") + 1])
        prediction_text = await asyncio.to_thread(prediction_path.read_text, encoding="utf-8")
        prediction = json.loads(prediction_text)
        model_dir = prediction["model_name_or_path"].replace("/", "__")
        report_resolved = self.mode not in {
            "unresolved",
            "infra-failure",
            "ambiguous-failure",
        }
        summary_resolved = report_resolved and self.mode != "mismatch"
        summary = {
            "total_instances": 1,
            "submitted_instances": 1,
            "completed_instances": 1,
            "resolved_instances": int(summary_resolved),
            "unresolved_instances": int(not summary_resolved),
            "error_instances": int(self.mode == "error-overlap"),
            "submitted_ids": [instance_id],
            "completed_ids": [instance_id],
            "resolved_ids": [instance_id] if summary_resolved else [],
            "unresolved_ids": [] if summary_resolved else [instance_id],
            "error_ids": [instance_id] if self.mode == "error-overlap" else [],
            "empty_patch_ids": [],
            "incomplete_ids": [],
            "infra_failure_ids": [instance_id] if self.mode == "infra-failure" else [],
            "ambiguous_failure_ids": ([instance_id] if self.mode == "ambiguous-failure" else []),
            "failure_reasons": (
                {instance_id: f"fixture {self.mode}"}
                if self.mode in {"infra-failure", "ambiguous-failure"}
                else {}
            ),
        }
        (cwd / f"{model_dir}.{run_id}.json").write_text(json.dumps(summary), encoding="utf-8")
        report_path = cwd / "logs" / "run_evaluation" / run_id / model_dir / instance_id
        report_path.mkdir(parents=True)
        (report_path / "report.json").write_text(
            json.dumps({instance_id: {"resolved": report_resolved}}), encoding="utf-8"
        )
        return CommandOutcome(
            0,
            f"Instances resolved: {int(summary_resolved)}\n",
            "",
            False,
        )


class _WorkspaceCommandRunner:
    def __init__(
        self,
        workspace: Path,
        base_commit: str,
        *,
        truncate_diff: bool = False,
    ) -> None:
        self.workspace = workspace
        self.base_commit = base_commit
        self.truncate_diff = truncate_diff
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def run(self, argv: tuple[str, ...], **options: Any) -> CommandOutcome:
        self.calls.append((argv, options))
        if argv[:3] == ("docker", "image", "inspect"):
            return CommandOutcome(0, "{}\n", "", False)
        if argv[:2] == ("docker", "create"):
            return CommandOutcome(0, "container-id\n", "", False)
        if argv[:2] == ("docker", "cp"):
            self.workspace.mkdir(parents=True)
            (self.workspace / "value.txt").write_text("before\n", encoding="utf-8")
            return CommandOutcome(0, "", "", False)
        if argv[:3] == ("git", "rev-parse", "HEAD"):
            return CommandOutcome(0, self.base_commit + "\n", "", False)
        if argv[:3] == ("git", "status", "--porcelain"):
            return CommandOutcome(0, "", "", False)
        if argv[:4] == ("git", "add", "-N", "--all"):
            return CommandOutcome(0, "", "", False)
        if argv[:3] == ("git", "diff", "--check"):
            return CommandOutcome(0, "", "", False)
        if argv[:3] == ("git", "diff", "--binary"):
            return CommandOutcome(
                0,
                (
                    "diff --git a/value.txt b/value.txt\n"
                    "--- a/value.txt\n"
                    "+++ b/value.txt\n"
                    "@@ -1 +1 @@\n-before\n+after\n"
                ),
                "",
                self.truncate_diff,
            )
        return CommandOutcome(0, "", "", False)


def test_swebench_cli_rejects_invalid_instance_before_creating_artifacts(
    tmp_path: Path,
    capsys: Any,
) -> None:
    artifacts = tmp_path / "artifacts"

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "../not-a-verified-instance",
            "--artifacts",
            str(artifacts),
        ]
    )

    assert exit_code == 2
    assert _records(capsys.readouterr().out) == [
        {
            "swebench": {
                "stage": "configuration",
                "status": "prediction_invalid",
                "diagnostic": ("instance ID must use the official owner__repo-number form"),
                "artifacts": None,
            }
        }
    ]
    assert not artifacts.exists()


@pytest.mark.parametrize(
    ("option", "value"),
    [("--timeout", "0"), ("--harness-timeout", "nan")],
)
def test_swebench_cli_rejects_nonpositive_or_nonfinite_timeouts_before_artifacts(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    option: str,
    value: str,
) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-placeholder")

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "synthetic__fixture-1",
            "--artifacts",
            str(artifacts),
            option,
            value,
        ]
    )

    assert exit_code == 2
    [record] = _records(capsys.readouterr().out)
    assert record["swebench"]["stage"] == "configuration"
    assert record["swebench"]["status"] == "prediction_invalid"
    assert "finite positive seconds" in record["swebench"]["diagnostic"]
    assert record["swebench"]["artifacts"] is None
    assert not artifacts.exists()


def test_artifact_manifest_does_not_enumerate_evaluator_workspace(tmp_path: Path) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    artifacts.write_json("config.json", {"instance_id": "synthetic__fixture-1"})
    workspace_file = artifacts.root / "workspace" / "large-repository" / "tracked.py"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("print('fixture')\n", encoding="utf-8")

    artifacts.finalize(status="no_patch", stage="prediction", exit_code=6, diagnostic="test")

    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"] == ["config.json"]


def test_swebench_cli_missing_key_fails_before_external_preparation(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "synthetic__fixture-1",
            "--artifacts",
            str(artifacts),
        ]
    )

    assert exit_code == 2
    assert _records(capsys.readouterr().out) == [
        {
            "swebench": {
                "stage": "provider_configuration",
                "status": "agent_failed",
                "diagnostic": "DEEPSEEK_API_KEY is required to use the DeepSeek provider.",
                "artifacts": None,
            }
        }
    ]
    assert not artifacts.exists()


def test_swebench_cli_docker_daemon_failure_is_auditable_and_secret_free(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    artifacts = tmp_path / "artifacts"
    secret = "test-only-swebench-provider-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    runner = _ScriptedCommandRunner(
        CommandOutcome(0, "Docker version 29.7.2\n", "", False),
        CommandOutcome(1, "", "Docker Desktop Linux daemon is unavailable", False),
    )

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "synthetic__fixture-1",
            "--artifacts",
            str(artifacts),
        ],
        swebench_command_runner=runner,
    )

    assert exit_code == 3
    assert _records(capsys.readouterr().out) == [
        {
            "swebench": {
                "stage": "environment_preparation",
                "status": "environment_preparation_failed",
                "diagnostic": (
                    "Docker CLI is available, but the Linux daemon is unavailable. "
                    "Start Docker Desktop manually and select Linux containers."
                ),
                "artifacts": str(artifacts.resolve()),
            }
        }
    ]
    assert [call[0] for call in runner.calls] == [
        ("docker", "--version"),
        ("docker", "info", "--format", "{{json .ServerVersion}}"),
    ]
    assert all("DEEPSEEK_API_KEY" not in call[1]["env"] for call in runner.calls)
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "environment_preparation_failed"
    assert manifest["completed"] is True
    assert manifest["stage"] == "environment_preparation"
    serialized = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in artifacts.rglob("*")
        if path.is_file()
    )
    assert secret not in serialized


def test_swebench_cli_finalizes_artifacts_when_instance_metadata_is_invalid(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    artifacts = tmp_path / "artifacts"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-placeholder")
    runner = _ScriptedCommandRunner(
        CommandOutcome(0, "Docker version 29.7.2\n", "", False),
        CommandOutcome(0, '"29.7.2"\n', "", False),
    )
    dependencies = SWEbenchDependencies(
        instance_loader=_MissingMetadataInstanceLoader(),
        workspace_factory=_PatchWorkspaceFactory(workspace, "unused"),
        harness_runner=_NeverHarness(),
    )

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "synthetic__fixture-1",
            "--artifacts",
            str(artifacts),
        ],
        swebench_command_runner=runner,
        swebench_dependencies=dependencies,
    )

    assert exit_code == 3
    assert _records(capsys.readouterr().out)[-1]["swebench"]["status"] == (
        "environment_preparation_failed"
    )
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == "environment_preparation_failed"
    assert "KeyError" in manifest["diagnostic"]


def test_swebench_cli_turns_preflight_keyboard_interrupt_into_cancelled_manifest(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-placeholder")

    class InterruptingCommandRunner:
        async def run(self, argv: tuple[str, ...], **options: Any) -> CommandOutcome:
            del argv, options
            raise KeyboardInterrupt

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "synthetic__fixture-1",
            "--artifacts",
            str(artifacts),
        ],
        swebench_command_runner=InterruptingCommandRunner(),
    )

    assert exit_code == 5
    assert _records(capsys.readouterr().out)[-1]["swebench"]["status"] == "cancelled"
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["stage"] == "environment_preparation"
    assert manifest["status"] == "cancelled"


def test_strict_process_runner_bounds_output_and_terminates_timeout() -> None:
    runner = SubprocessCommandRunner()

    bounded = asyncio.run(
        runner.run(
            (sys.executable, "-c", "print('0123456789', end='')"),
            cwd=None,
            env=None,
            timeout_seconds=10,
            cancel_event=None,
            on_chunk=None,
            output_limit_bytes=5,
        )
    )

    assert bounded.stdout == "01234"
    assert bounded.output_truncated is True
    started = time.monotonic()
    with pytest.raises(CommandTimeoutError, match="timed out"):
        asyncio.run(
            runner.run(
                (sys.executable, "-c", "import time; time.sleep(10)"),
                cwd=None,
                env=None,
                timeout_seconds=0.05,
                cancel_event=None,
                on_chunk=None,
                output_limit_bytes=1024,
            )
        )
    assert time.monotonic() - started < 3


def test_docker_workspace_uses_preloaded_image_owned_mount_and_cleanup(tmp_path: Path) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = artifacts.root / "workspace"
    base_commit = "a" * 40
    runner = _WorkspaceCommandRunner(workspace, base_commit)
    factory = DockerWorkspaceFactory(runner)
    instance = SWEbenchInstance(
        instance_id="synthetic__fixture-1",
        repo="synthetic/fixture",
        base_commit=base_commit,
        problem_statement="fixture",
        image="swebench/sweb.eval.synthetic_fixture:latest",
        container_workdir="/testbed",
        container_user="root",
        dataset_revision="fixture-revision",
        harness_version="fixture-harness",
        dataset_record={
            "instance_id": "synthetic__fixture-1",
            "repo": "synthetic/fixture",
            "base_commit": base_commit,
            "problem_statement": "fixture",
        },
    )

    async def exercise() -> str:
        prepared = await factory.prepare(instance, artifacts)
        await prepared.environment.edit_text("value.txt", "before", "after")
        patch = await prepared.collect_patch()
        await prepared.close()
        return patch

    patch = asyncio.run(exercise())

    assert patch.startswith("diff --git a/value.txt b/value.txt\n")
    argvs = [call[0] for call in runner.calls]
    assert ("docker", "image", "inspect", instance.image) in argvs
    assert not any(argv[:2] in {("docker", "pull"), ("docker", "build")} for argv in argvs)
    assert (
        "git",
        "diff",
        "--binary",
        "--no-ext-diff",
        base_commit,
        "--",
        ".",
    ) in argvs
    agent_create = next(
        argv for argv in argvs if argv[:2] == ("docker", "create") and "--mount" in argv
    )
    mount = agent_create[agent_create.index("--mount") + 1]
    assert mount == f"type=bind,source={workspace.resolve()},target=/testbed"
    assert "--network" in agent_create
    assert agent_create[agent_create.index("--network") + 1] == "none"
    assert "--env" not in agent_create
    removed = [argv for argv in argvs if argv[:3] == ("docker", "rm", "--force")]
    assert len(removed) == 2
    assert all("DEEPSEEK_API_KEY" not in call[1]["env"] for call in runner.calls)


def test_docker_workspace_rejects_truncated_prediction_patch(tmp_path: Path) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = artifacts.root / "workspace"
    base_commit = "a" * 40
    runner = _WorkspaceCommandRunner(workspace, base_commit, truncate_diff=True)
    instance = asyncio.run(_FixtureInstanceLoader().load("synthetic__fixture-1"))

    async def exercise() -> None:
        prepared = await DockerWorkspaceFactory(runner).prepare(instance, artifacts)
        try:
            with pytest.raises(SWEbenchConfigurationError, match="output was truncated"):
                await prepared.collect_patch()
        finally:
            await prepared.close()

    asyncio.run(exercise())


def test_official_instance_loader_pins_verified_revision_and_hides_host_secrets(
    monkeypatch: Any,
) -> None:
    secret = "must-not-reach-datasets"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    calls: list[tuple[str, str, str]] = []

    def load_dataset(name: str, *, split: str, revision: str) -> list[dict[str, str]]:
        calls.append((name, split, revision))
        assert "DEEPSEEK_API_KEY" not in __import__("os").environ
        return [
            {
                "instance_id": "synthetic__fixture-1",
                "repo": "synthetic/fixture",
                "base_commit": "c" * 40,
                "problem_statement": "Fix the fixture.",
            }
        ]

    modules = {
        "datasets": SimpleNamespace(load_dataset=load_dataset),
        "swebench.harness.utils": SimpleNamespace(
            make_test_spec=lambda datum: SimpleNamespace(
                image=f"official/{datum['instance_id']}:latest"
            )
        ),
        "swebench.image_builder.constants": SimpleNamespace(
            CONTAINER_WORKDIR="/testbed",
            CONTAINER_USER="root",
        ),
    }
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "5.0.2")
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name])

    instance = asyncio.run(OfficialInstanceLoader().load("synthetic__fixture-1"))

    assert calls == [(SWE_BENCH_DATASET, SWE_BENCH_SPLIT, SWE_BENCH_DATASET_REVISION)]
    assert instance.image == "official/synthetic__fixture-1:latest"
    assert instance.dataset_revision == SWE_BENCH_DATASET_REVISION
    assert instance.harness_version == "5.0.2"
    assert instance.dataset_record["problem_statement"] == "Fix the fixture."
    assert __import__("os").environ["DEEPSEEK_API_KEY"] == secret


def test_official_instance_loader_rejects_unverified_harness_version(
    monkeypatch: Any,
) -> None:
    datum = {
        "instance_id": "synthetic__fixture-1",
        "repo": "synthetic/fixture",
        "base_commit": "c" * 40,
        "problem_statement": "Fix the fixture.",
    }
    modules = {
        "datasets": SimpleNamespace(load_dataset=lambda *args, **kwargs: [datum]),
        "swebench.harness.utils": SimpleNamespace(
            make_test_spec=lambda item: SimpleNamespace(image="official/fixture:latest")
        ),
        "swebench.image_builder.constants": SimpleNamespace(
            CONTAINER_WORKDIR="/testbed",
            CONTAINER_USER="root",
        ),
    }
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "5.0.3")
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name])

    with pytest.raises(RuntimeError, match="unsupported SWE-bench version 5.0.3"):
        asyncio.run(OfficialInstanceLoader().load("synthetic__fixture-1"))


def test_pinned_swebench_runtime_exports_official_test_spec_builder() -> None:
    try:
        harness_utils = importlib.import_module("swebench.harness.utils")
    except ImportError:
        pytest.skip("install the swebench optional dependency for this contract test")

    assert importlib.metadata.version("swebench") == "5.0.2"
    assert callable(harness_utils.make_test_spec)


@pytest.mark.skipif(
    os.environ.get("CODING_AGENT_RUN_NETWORK_TESTS") != "1",
    reason="set CODING_AGENT_RUN_NETWORK_TESTS=1 to verify the pinned official dataset row",
)
def test_pinned_verified_row_builds_swebench_5_0_2_test_spec() -> None:
    try:
        datasets = importlib.import_module("datasets")
        harness_utils = importlib.import_module("swebench.harness.utils")
    except ImportError:
        pytest.skip("install the swebench optional dependency for this network contract test")

    dataset = datasets.load_dataset(
        SWE_BENCH_DATASET,
        split=f"{SWE_BENCH_SPLIT}[:1]",
        revision=SWE_BENCH_DATASET_REVISION,
    )
    datum = dict(dataset[0])
    spec = harness_utils.make_test_spec(datum)

    assert SWE_BENCH_DATASET == "SWE-bench/SWE-bench_Verified"
    assert SWE_BENCH_DATASET_REVISION == "78f471bf655a3137b2e8a75af1501690ec009ec3"
    assert {
        "instance_id",
        "image",
        "repo",
        "version",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "log_parser",
        "eval_type",
        "eval_script",
    } <= datum.keys()
    assert datum["instance_id"] == "astropy__astropy-12907"
    assert spec.image == datum["image"]
    assert spec.image == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"


@pytest.mark.parametrize(
    ("patch", "expected_status", "expected_diagnostic"),
    [
        ("", "no_patch", "workspace produced no git patch"),
        ("not a git diff\n", "prediction_invalid", "workspace patch is not a git diff"),
    ],
)
def test_evaluator_never_invokes_harness_without_a_valid_git_prediction(
    tmp_path: Path,
    patch: str,
    expected_status: str,
    expected_diagnostic: str,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(workspace, patch)
    harness = _NeverHarness()
    evaluator = SWEbenchEvaluator(
        FakeProvider.streamed_run(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=harness,
        ),
    )

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == expected_status
    assert result.diagnostic == expected_diagnostic
    assert result.exit_code != 0
    assert harness.called is False
    assert workspace_factory.prepared.closed is True
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == expected_status


@pytest.mark.parametrize(
    ("patch", "expected_status"),
    [
        ("", "no_patch"),
        (
            "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n",
            "environment_preparation_failed",
        ),
    ],
)
def test_evaluator_finalizes_when_workspace_cleanup_fails(
    tmp_path: Path,
    patch: str,
    expected_status: str,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _FailingCloseWorkspaceFactory(workspace, patch)
    harness = _NeverHarness()
    evaluator = SWEbenchEvaluator(
        FakeProvider.streamed_run(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=harness,
        ),
    )

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == expected_status
    assert harness.called is False
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == expected_status
    assert "cleanup_failures.jsonl" in manifest["artifacts"]
    [cleanup] = _records((artifacts.root / "cleanup_failures.jsonl").read_text(encoding="utf-8"))
    assert cleanup["operation"] == "workspace.close"
    assert cleanup["error_type"] == "OSError"
    assert cleanup["diagnostic"] == "workspace cleanup exploded"


def test_evaluator_finalizes_when_workspace_cleanup_is_cancelled(tmp_path: Path) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _CancelledCloseWorkspaceFactory(workspace, "")
    evaluator = SWEbenchEvaluator(
        FakeProvider.streamed_run(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=_NeverHarness(),
        ),
    )

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == "no_patch"
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == "no_patch"
    [cleanup] = _records((artifacts.root / "cleanup_failures.jsonl").read_text(encoding="utf-8"))
    assert cleanup["operation"] == "workspace.close"
    assert cleanup["error_type"] == "CancelledError"


def test_evaluator_reports_cancelled_when_pre_harness_workspace_close_is_cancelled(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _CancelledCloseWorkspaceFactory(
        workspace,
        "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n",
    )
    harness = _NeverHarness()
    evaluator = SWEbenchEvaluator(
        FakeProvider.streamed_run(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=harness,
        ),
    )

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == "cancelled"
    assert result.stage == "prediction"
    assert harness.called is False
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == "cancelled"


def test_evaluator_preserves_primary_result_when_cleanup_audit_write_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _FailingCloseWorkspaceFactory(workspace, "")
    evaluator = SWEbenchEvaluator(
        FakeProvider.streamed_run(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=_NeverHarness(),
        ),
    )
    original_append_jsonl = ArtifactBundle.append_jsonl

    def fail_cleanup_audit(
        self: ArtifactBundle,
        relative: str,
        value: object,
    ) -> Path:
        if relative == "cleanup_failures.jsonl":
            raise OSError("cleanup audit disk failure")
        return original_append_jsonl(self, relative, value)

    monkeypatch.setattr(ArtifactBundle, "append_jsonl", fail_cleanup_audit)

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == "no_patch"
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == "no_patch"


def test_evaluator_runs_from_source_tree_without_distribution_metadata(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(workspace, "")
    evaluator = SWEbenchEvaluator(
        FakeProvider.streamed_run(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=_NeverHarness(),
        ),
    )

    def missing_distribution(name: str) -> str:
        assert name == "coding-agent-kernel"
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_distribution)

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == "no_patch"
    kernel_configuration = json.loads(
        (artifacts.root / "kernel_configuration.json").read_text(encoding="utf-8")
    )
    assert kernel_configuration["kernel_distribution_version"] == "source-tree"


@pytest.mark.parametrize(
    ("provider", "timeout_seconds", "expected_status"),
    [
        (FakeProvider.provider_error(), 10.0, "agent_failed"),
        (_BlockingProvider(), 0.05, "timed_out"),
    ],
)
def test_evaluator_distinguishes_agent_failure_and_timeout(
    tmp_path: Path,
    provider: Any,
    timeout_seconds: float,
    expected_status: str,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(
        workspace,
        "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n",
    )
    harness = _NeverHarness()
    evaluator = SWEbenchEvaluator(
        provider,
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=harness,
        ),
    )

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=timeout_seconds,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=deny,
        )

    result = asyncio.run(run())

    assert result.status == expected_status
    assert result.exit_code != 0
    assert harness.called is False
    assert workspace_factory.prepared.closed is True
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == expected_status
    assert manifest["completed"] is True


def test_evaluator_closes_agent_session_when_permission_resolver_fails(tmp_path: Path) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(workspace, "unused")
    harness = _NeverHarness()
    provider = FakeProvider(
        (
            *_tool_call_events(
                0,
                "call-write",
                "write",
                {"path": "value.txt", "content": "after\n"},
            ),
            ProviderDone(),
        )
    )
    evaluator = SWEbenchEvaluator(
        provider,
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=harness,
        ),
    )

    async def run() -> Any:
        async def fail_permission(request_id: str) -> bool:
            del request_id
            raise RuntimeError("permission frontend disconnected")

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.ASK,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=fail_permission,
        )

    result = asyncio.run(run())

    assert result.status == "agent_failed"
    assert "permission frontend disconnected" in result.diagnostic
    assert workspace_factory.prepared.closed is True
    assert harness.called is False
    assert '"record_type":"closed"' in (artifacts.root / "session.jsonl").read_text(
        encoding="utf-8"
    )


def test_evaluator_preserves_primary_failure_when_agent_cleanup_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(workspace, "unused")
    provider = FakeProvider(
        (
            *_tool_call_events(
                0,
                "call-write",
                "write",
                {"path": "value.txt", "content": "after\n"},
            ),
            ProviderDone(),
        )
    )
    evaluator = SWEbenchEvaluator(
        provider,
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=_NeverHarness(),
        ),
    )

    def fail_close_session(self: AgentKernel) -> None:
        del self
        raise OSError("session cleanup exploded")

    monkeypatch.setattr(AgentKernel, "close_session", fail_close_session)

    async def run() -> Any:
        async def fail_permission(request_id: str) -> bool:
            del request_id
            raise RuntimeError("permission frontend disconnected")

        return await evaluator.run(
            SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.ASK,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            artifacts,
            permission_resolver=fail_permission,
        )

    result = asyncio.run(run())

    assert result.status == "agent_failed"
    assert "permission frontend disconnected" in result.diagnostic
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == "agent_failed"
    assert "cleanup_failures.jsonl" in manifest["artifacts"]
    [cleanup] = _records((artifacts.root / "cleanup_failures.jsonl").read_text(encoding="utf-8"))
    assert cleanup["operation"] == "kernel.close_session"
    assert cleanup["error_type"] == "OSError"
    assert cleanup["diagnostic"] == "session cleanup exploded"


@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
def test_evaluator_bounds_consumer_cleanup_when_agent_cancel_fails(
    tmp_path: Path,
    monkeypatch: Any,
    failure_type: type[BaseException],
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(workspace, "unused")
    evaluator = SWEbenchEvaluator(
        _BlockingProvider(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=_NeverHarness(),
        ),
    )

    async def fail_cancel(self: AgentRun) -> None:
        del self
        raise failure_type("agent cancellation exploded")

    monkeypatch.setattr(AgentRun, "cancel", fail_cancel)

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await asyncio.wait_for(
            evaluator.run(
                SWEbenchRunConfig(
                    instance_id="synthetic__fixture-1",
                    model="deepseek-v4-pro",
                    mode=PermissionMode.FULL,
                    agent_timeout_seconds=0.01,
                    harness_timeout_seconds=10,
                ),
                artifacts,
                permission_resolver=deny,
            ),
            timeout=0.5,
        )

    result = asyncio.run(run())

    assert result.status == "timed_out"
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed"] is True
    assert manifest["status"] == "timed_out"
    cleanup_records = _records(
        (artifacts.root / "cleanup_failures.jsonl").read_text(encoding="utf-8")
    )
    assert any(
        record["operation"] == "agent_run.cancel" and record["error_type"] == failure_type.__name__
        for record in cleanup_records
    )


def test_evaluator_bounds_agent_cancel_itself(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evaluator = SWEbenchEvaluator(
        _BlockingProvider(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=_PatchWorkspaceFactory(workspace, "unused"),
            harness_runner=_NeverHarness(),
        ),
    )

    async def block_cancel(self: AgentRun) -> None:
        del self
        await asyncio.Event().wait()

    monkeypatch.setattr(AgentRun, "cancel", block_cancel)
    monkeypatch.setattr("coding_agent.swebench._CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def run() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        return await asyncio.wait_for(
            evaluator.run(
                SWEbenchRunConfig(
                    instance_id="synthetic__fixture-1",
                    model="deepseek-v4-pro",
                    mode=PermissionMode.FULL,
                    agent_timeout_seconds=0.01,
                    harness_timeout_seconds=10,
                ),
                artifacts,
                permission_resolver=deny,
            ),
            timeout=0.5,
        )

    result = asyncio.run(run())

    assert result.status == "timed_out"
    cleanup_records = _records(
        (artifacts.root / "cleanup_failures.jsonl").read_text(encoding="utf-8")
    )
    assert any(
        record["operation"] == "agent_run.cancel" and record["error_type"] == "TimeoutError"
        for record in cleanup_records
    )


def test_evaluator_interruption_is_cancelled_auditable_and_closes_workspace(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_factory = _PatchWorkspaceFactory(workspace, "unused")
    harness = _NeverHarness()
    evaluator = SWEbenchEvaluator(
        _BlockingProvider(),
        SWEbenchDependencies(
            instance_loader=_FixtureInstanceLoader(),
            workspace_factory=workspace_factory,
            harness_runner=harness,
        ),
    )

    async def scenario() -> Any:
        async def deny(request_id: str) -> bool:
            del request_id
            return False

        task = asyncio.create_task(
            evaluator.run(
                SWEbenchRunConfig(
                    instance_id="synthetic__fixture-1",
                    model="deepseek-v4-pro",
                    mode=PermissionMode.FULL,
                    agent_timeout_seconds=30,
                    harness_timeout_seconds=30,
                ),
                artifacts,
                permission_resolver=deny,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        return await task

    result = asyncio.run(scenario())

    assert result.status == "cancelled"
    assert result.exit_code != 0
    assert workspace_factory.prepared.closed is True
    assert harness.called is False
    manifest = json.loads((artifacts.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert manifest["completed"] is True
    assert '"record_type":"closed"' in (artifacts.root / "session.jsonl").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_resolved"),
    [
        ("invocation-failed", "harness_invocation_failed", None),
        ("missing-result", "harness_rejected", None),
        ("unresolved", "harness_failed", False),
        ("mismatch", "harness_rejected", None),
        ("error-overlap", "harness_rejected", None),
        ("infra-failure", "harness_rejected", None),
        ("ambiguous-failure", "harness_rejected", None),
    ],
)
def test_official_harness_runner_preserves_failure_and_rejection_semantics(
    tmp_path: Path,
    mode: str,
    expected_status: str,
    expected_resolved: bool | None,
) -> None:
    artifacts = ArtifactBundle.create(tmp_path / "artifacts")
    prediction_path = artifacts.write_text(
        "prediction.jsonl",
        json.dumps(
            {
                "instance_id": "synthetic__fixture-1",
                "model_name_or_path": "deepseek-v4-pro",
                "model_patch": "diff --git a/a b/a\n--- a/a\n+++ b/a\n",
            }
        )
        + "\n",
    )
    runner = OfficialHarnessRunner(_OfficialResultCommandRunner(mode))

    result = asyncio.run(
        runner.evaluate(
            config=SWEbenchRunConfig(
                instance_id="synthetic__fixture-1",
                model="deepseek-v4-pro",
                mode=PermissionMode.FULL,
                agent_timeout_seconds=10,
                harness_timeout_seconds=10,
            ),
            instance=asyncio.run(_FixtureInstanceLoader().load("synthetic__fixture-1")),
            prediction_path=prediction_path,
            artifacts=artifacts,
            run_id="fixture-run",
        )
    )

    assert result.status == expected_status
    assert result.resolved is expected_resolved
    assert result.exit_code != 0


def test_container_environment_contains_files_and_runs_bash_with_strict_docker_argv(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "value.txt").write_text("before\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-cross-process-seam")
    runner = _ScriptedCommandRunner(CommandOutcome(0, "tests passed\n", "", False))
    environment = ContainerCodingEnvironment(
        workspace,
        container_name="sweb-agent-synthetic-fixture-1",
        command_runner=runner,
        container_workdir="/testbed",
    )

    async def exercise() -> None:
        assert await environment.read_text("pkg/value.txt") == "before\n"
        assert await environment.edit_text("pkg/value.txt", "before", "after") == 1
        result = await environment.run_command(
            "python -m pytest -q",
            cwd="pkg",
            timeout_seconds=42,
        )
        assert result.stdout == "tests passed\n"
        with pytest.raises(WorkspacePathError):
            await environment.write_text("../escape.txt", "blocked")

    asyncio.run(exercise())

    argv, options = runner.calls[0]
    assert argv == (
        "docker",
        "exec",
        "--workdir",
        "/testbed/pkg",
        "sweb-agent-synthetic-fixture-1",
        "/bin/bash",
        "-lc",
        "python -m pytest -q",
    )
    assert options["cwd"] is None
    assert options["timeout_seconds"] == 42
    assert "DEEPSEEK_API_KEY" not in options["env"]
    assert (workspace / "pkg" / "value.txt").read_text(encoding="utf-8") == "after\n"
    assert not (tmp_path / "escape.txt").exists()


def test_swebench_cli_drives_kernel_prediction_and_official_result_ingestion(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    artifacts = tmp_path / "artifacts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("before\n", encoding="utf-8")
    secret = "test-only-full-evaluator-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    responses = iter(
        (
            _SSEStream(
                {
                    "id": "fixture-tool",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "edit-value",
                                        "type": "function",
                                        "function": {
                                            "name": "edit",
                                            "arguments": (
                                                '{"path":"value.txt","old":"before","new":"after"}'
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
                "[DONE]",
            ),
            _SSEStream(
                {
                    "id": "fixture-final",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "Updated and verified value.txt."},
                            "finish_reason": "stop",
                        }
                    ],
                },
                "[DONE]",
            ),
        )
    )
    request_bodies: list[dict[str, Any]] = []

    async def provider_handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads((await request.aread()).decode()))
        return httpx.Response(200, stream=next(responses))

    command_runner = _OfficialResultCommandRunner()
    workspace_factory = _FixtureWorkspaceFactory(workspace)
    dependencies = SWEbenchDependencies(
        instance_loader=_FixtureInstanceLoader(),
        workspace_factory=workspace_factory,
        harness_runner=OfficialHarnessRunner(command_runner),
    )

    exit_code = main(
        [
            "swebench",
            "run",
            "--instance",
            "synthetic__fixture-1",
            "--artifacts",
            str(artifacts),
            "--mode",
            "full",
            "--timeout",
            "30",
            "--harness-timeout",
            "60",
        ],
        deepseek_transport=httpx.MockTransport(provider_handler),
        swebench_command_runner=command_runner,
        swebench_dependencies=dependencies,
    )

    records = _records(capsys.readouterr().out)
    assert exit_code == 0
    assert records[-1]["swebench"] == {
        "stage": "complete",
        "status": "success",
        "diagnostic": "official Harness resolved the instance",
        "artifacts": str(artifacts.resolve()),
    }
    assert request_bodies[0]["messages"][-1]["content"] == (
        "Change value.txt from before to after and verify it."
    )
    prediction = json.loads((artifacts / "prediction.jsonl").read_text(encoding="utf-8"))
    assert set(prediction) == {"instance_id", "model_name_or_path", "model_patch"}
    assert prediction["instance_id"] == "synthetic__fixture-1"
    assert prediction["model_name_or_path"] == "deepseek-v4-pro"
    assert prediction["model_patch"].startswith("diff --git a/value.txt b/value.txt\n")
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["completed"] is True
    assert (artifacts / "session.jsonl").is_file()
    assert (artifacts / "events.jsonl").is_file()
    assert (artifacts / "tool_results.jsonl").is_file()
    kernel_configuration = json.loads(
        (artifacts / "kernel_configuration.json").read_text(encoding="utf-8")
    )
    assert kernel_configuration["enabled_tools"] == [
        "read",
        "write",
        "edit",
        "bash",
        "grep",
        "find",
        "ls",
    ]
    assert kernel_configuration["context_settings"]["project_context"] == [
        "Workspace root: /testbed"
    ]
    assert (artifacts / "workspace.patch").read_text(encoding="utf-8") == (
        prediction["model_patch"]
    )
    assert (artifacts / "harness_summary.json").is_file()
    assert (artifacts / "harness_instance_result.json").is_file()
    assert workspace_factory.prepared.closed is True
    harness_call = command_runner.calls[-1]
    assert "DEEPSEEK_API_KEY" not in harness_call[1]["env"]
    dataset_path = Path(harness_call[0][harness_call[0].index("--dataset_name") + 1])
    assert dataset_path == artifacts / "official_instance.json"
    assert json.loads(dataset_path.read_text(encoding="utf-8"))[0]["instance_id"] == (
        "synthetic__fixture-1"
    )
    serialized = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in artifacts.rglob("*")
        if path.is_file()
    )
    assert secret not in serialized
