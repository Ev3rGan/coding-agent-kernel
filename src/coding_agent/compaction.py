"""Typed Context Compaction checkpoints and their canonical v2 wire contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from coding_agent.events import TokenUsage

COMPACTION_SUMMARY_HEADINGS: Final = (
    "Goal",
    "Constraints and preferences",
    "Done and verified",
    "In progress",
    "Blocked",
    "Key decisions and relevant rejected approaches",
    "Exact files, symbols, commands and errors",
    "Next steps",
)


@dataclass(frozen=True, slots=True)
class CompactionCommandEvidence:
    command: str
    cwd: str | None
    exit_status: int | None

    def record(self) -> dict[str, object]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_status": self.exit_status,
        }


@dataclass(frozen=True, slots=True)
class CompactionToolErrorEvidence:
    call_id: str
    code: str

    def record(self) -> dict[str, str]:
        return {"call_id": self.call_id, "code": self.code}


@dataclass(frozen=True, slots=True)
class CompactionEvidence:
    read_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    commands: tuple[CompactionCommandEvidence, ...] = ()
    tool_errors: tuple[CompactionToolErrorEvidence, ...] = ()

    def record(self) -> dict[str, object]:
        return {
            "read_files": list(self.read_files),
            "modified_files": list(self.modified_files),
            "commands": [command.record() for command in self.commands],
            "tool_errors": [error.record() for error in self.tool_errors],
        }


@dataclass(frozen=True, slots=True)
class CompactionStrategy:
    name: str = "semantic-checkpoint"
    revision: str = "2"

    def record(self) -> dict[str, str]:
        return {"name": self.name, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class CompactionMetrics:
    characters_before: int = 0
    characters_after: int = 0
    covered_count: int = 0
    retained_count: int = 0
    compaction_depth: int = 0
    summary_usage: TokenUsage | None = None
    trigger: Literal["automatic", "manual"] = "automatic"
    thrashing_detected: bool = False

    def record(self) -> dict[str, object]:
        return {
            "characters_before": self.characters_before,
            "characters_after": self.characters_after,
            "covered_count": self.covered_count,
            "retained_count": self.retained_count,
            "compaction_depth": self.compaction_depth,
            "summary_usage": (
                None
                if self.summary_usage is None
                else {
                    "input_tokens": self.summary_usage.input_tokens,
                    "output_tokens": self.summary_usage.output_tokens,
                }
            ),
            "trigger": self.trigger,
            "thrashing_detected": self.thrashing_detected,
        }


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """One validated checkpoint proposal for atomic Session persistence."""

    covered_entry_ids: tuple[str, ...]
    summary: str
    version: int = 2
    first_kept_entry_id: str | None = None
    previous_checkpoint_id: str | None = None
    evidence: CompactionEvidence = field(default_factory=CompactionEvidence)
    strategy: CompactionStrategy = field(default_factory=CompactionStrategy)
    metrics: CompactionMetrics = field(default_factory=CompactionMetrics)

    def record(self) -> dict[str, object]:
        base: dict[str, object] = {
            "version": self.version,
            "covered_entry_ids": list(self.covered_entry_ids),
            "summary": self.summary,
        }
        if self.version == 1:
            return base
        base.update(
            {
                "first_kept_entry_id": self.first_kept_entry_id,
                "previous_checkpoint_id": self.previous_checkpoint_id,
                "evidence": self.evidence.record(),
                "strategy": self.strategy.record(),
                "metrics": self.metrics.record(),
            }
        )
        return base


@dataclass(frozen=True, slots=True)
class CompactionCheckpoint:
    entry_id: str
    version: int
    summary: str
    covered_entry_ids: tuple[str, ...]
    first_kept_entry_id: str | None
    previous_checkpoint_id: str | None
    evidence: CompactionEvidence
    strategy: CompactionStrategy
    metrics: CompactionMetrics


class CompactionContractError(ValueError):
    """A persisted or proposed checkpoint violates the canonical v2 contract."""


def validate_compaction_summary(
    summary: object,
    *,
    max_characters: int | None = None,
) -> str:
    """Return a summary only when it matches the canonical semantic schema."""

    if not isinstance(summary, str) or not summary.strip():
        raise CompactionContractError("has an invalid summary")
    if max_characters is not None and len(summary) > max_characters:
        raise CompactionContractError("summary exceeds its character budget")
    headings = tuple(line[3:].strip() for line in summary.splitlines() if line.startswith("## "))
    if headings != COMPACTION_SUMMARY_HEADINGS:
        raise CompactionContractError("has an invalid summary structure")
    return summary


def decode_v2_compaction_checkpoint(
    entry_id: str,
    payload: Mapping[str, object],
    *,
    message_ids: tuple[str, ...],
    previous_checkpoint_id: str | None,
    previous_coverage: tuple[str, ...],
    expected_depth: int,
) -> CompactionCheckpoint:
    """Validate and decode one v2 payload against its Active Branch relationship."""

    if payload.get("version") != 2:
        raise CompactionContractError("must use version 2")
    summary = validate_compaction_summary(payload.get("summary"))
    coverage = _string_tuple(payload.get("covered_entry_ids"), "covered_entry_ids")
    if not coverage or coverage != message_ids[: len(coverage)]:
        raise CompactionContractError("has illegal coverage")
    if coverage[: len(previous_coverage)] != previous_coverage or len(coverage) <= len(
        previous_coverage
    ):
        raise CompactionContractError("does not advance prior coverage")

    first_kept = payload.get("first_kept_entry_id")
    expected_first_kept = None if len(coverage) == len(message_ids) else message_ids[len(coverage)]
    if first_kept != expected_first_kept:
        raise CompactionContractError("has an illegal first-kept boundary")
    raw_previous_id = payload.get("previous_checkpoint_id")
    if raw_previous_id != previous_checkpoint_id:
        raise CompactionContractError("has invalid checkpoint lineage")

    evidence = _decode_evidence(payload.get("evidence"))
    strategy = _decode_strategy(payload.get("strategy"))
    metrics = _decode_metrics(
        payload.get("metrics"),
        covered_count=len(coverage),
        retained_count=len(message_ids) - len(coverage),
        expected_depth=expected_depth,
    )
    return CompactionCheckpoint(
        entry_id,
        2,
        summary,
        coverage,
        first_kept,
        raw_previous_id,
        evidence,
        strategy,
        metrics,
    )


def _decode_evidence(value: object) -> CompactionEvidence:
    if not isinstance(value, dict):
        raise CompactionContractError("has invalid evidence")
    read_files = _string_tuple(value.get("read_files"), "read_files")
    modified_files = _string_tuple(value.get("modified_files"), "modified_files")
    raw_commands = value.get("commands")
    if not isinstance(raw_commands, list):
        raise CompactionContractError("has invalid command evidence")
    commands: list[CompactionCommandEvidence] = []
    for command in raw_commands:
        if (
            not isinstance(command, dict)
            or not isinstance(command.get("command"), str)
            or (command.get("cwd") is not None and not isinstance(command.get("cwd"), str))
            or (
                command.get("exit_status") is not None
                and type(command.get("exit_status")) is not int
            )
        ):
            raise CompactionContractError("has invalid command evidence")
        commands.append(
            CompactionCommandEvidence(
                command["command"],
                cast(str | None, command.get("cwd")),
                cast(int | None, command.get("exit_status")),
            )
        )
    raw_errors = value.get("tool_errors")
    if not isinstance(raw_errors, list):
        raise CompactionContractError("has invalid Tool error evidence")
    errors: list[CompactionToolErrorEvidence] = []
    for error in raw_errors:
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("call_id"), str)
            or not isinstance(error.get("code"), str)
        ):
            raise CompactionContractError("has invalid Tool error evidence")
        errors.append(CompactionToolErrorEvidence(error["call_id"], error["code"]))
    return CompactionEvidence(read_files, modified_files, tuple(commands), tuple(errors))


def _decode_strategy(value: object) -> CompactionStrategy:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("name"), str)
        or not value["name"]
        or not isinstance(value.get("revision"), str)
        or not value["revision"]
    ):
        raise CompactionContractError("has invalid strategy metadata")
    return CompactionStrategy(value["name"], value["revision"])


def _decode_metrics(
    value: object,
    *,
    covered_count: int,
    retained_count: int,
    expected_depth: int,
) -> CompactionMetrics:
    if not isinstance(value, dict):
        raise CompactionContractError("has invalid metrics")
    integer_names = (
        "characters_before",
        "characters_after",
        "covered_count",
        "retained_count",
        "compaction_depth",
    )
    if any(
        type(value.get(name)) is not int or cast(int, value.get(name)) < 0 for name in integer_names
    ):
        raise CompactionContractError("has invalid metrics")
    characters_before = cast(int, value["characters_before"])
    characters_after = cast(int, value["characters_after"])
    trigger = value.get("trigger")
    thrashing = value.get("thrashing_detected")
    if (
        characters_after > characters_before
        or value.get("covered_count") != covered_count
        or value.get("retained_count") != retained_count
        or value.get("compaction_depth") != expected_depth
        or trigger not in {"automatic", "manual"}
        or thrashing is not False
    ):
        raise CompactionContractError("has invalid lifecycle metrics")
    usage_value = value.get("summary_usage")
    usage = None
    if usage_value is not None:
        if (
            not isinstance(usage_value, dict)
            or type(usage_value.get("input_tokens")) is not int
            or cast(int, usage_value.get("input_tokens")) < 0
            or type(usage_value.get("output_tokens")) is not int
            or cast(int, usage_value.get("output_tokens")) < 0
        ):
            raise CompactionContractError("has invalid summary usage")
        usage = TokenUsage(usage_value["input_tokens"], usage_value["output_tokens"])
    return CompactionMetrics(
        characters_before=characters_before,
        characters_after=characters_after,
        covered_count=covered_count,
        retained_count=retained_count,
        compaction_depth=expected_depth,
        summary_usage=usage,
        trigger=cast(Literal["automatic", "manual"], trigger),
        thrashing_detected=False,
    )


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise CompactionContractError(f"has invalid {label}")
    return tuple(value)
