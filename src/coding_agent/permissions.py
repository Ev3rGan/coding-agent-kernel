"""Host-owned Permission Mode, Operation Intent, and one-time decision values."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from coding_agent.json_contract import json_object_snapshot


class PermissionMode(StrEnum):
    """Run-scoped authority selected by a Host."""

    PLAN = "plan"
    ASK = "ask"
    AUTO = "auto"
    FULL = "full"


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class OperationKind(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NETWORK = "network"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class TargetScope(StrEnum):
    WORKSPACE = "workspace"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"


class ToolCallLike(Protocol):
    @property
    def call_id(self) -> str: ...

    @property
    def tool_name(self) -> str: ...

    @property
    def arguments(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OperationIntent:
    """Normalized targets and possible side effects for final ToolCall arguments."""

    tool_name: str
    kind: OperationKind
    scope: TargetScope
    targets: tuple[str, ...]
    reason: str
    command: str | None = None
    cwd: str | None = None

    def record(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "targets": list(self.targets),
            "reason": self.reason,
            "command": self.command,
            "cwd": self.cwd,
        }

    def audit_record(self) -> dict[str, object]:
        """Return a durable form that omits the possibly secret shell command."""

        return {
            "tool_name": self.tool_name,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "targets": list(self.targets),
            "reason": self.reason,
            "cwd": self.cwd,
            "command_sha256": (
                None
                if self.command is None
                else hashlib.sha256(self.command.encode("utf-8")).hexdigest()
            ),
        }


@dataclass(frozen=True, slots=True)
class PermissionEvaluation:
    action: PermissionAction
    intent: OperationIntent
    binding: str
    reason: str
    final_arguments_json: str

    @property
    def final_arguments(self) -> dict[str, Any]:
        value = json.loads(self.final_arguments_json)
        if not isinstance(value, dict):  # pragma: no cover - constructed canonically
            raise RuntimeError("Permission arguments must be a JSON object.")
        return value


PermissionResolution = Literal["approved", "denied"]
PermissionDecisionSource = Literal["policy", "host"]


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A transient one-time request bound to one final ToolCall and Operation Intent."""

    request_id: str
    run_id: str
    call_id: str
    tool_name: str
    final_arguments_json: str
    intent: OperationIntent
    binding: str
    reason: str

    @property
    def final_arguments(self) -> dict[str, Any]:
        value = json.loads(self.final_arguments_json)
        if not isinstance(value, dict):  # pragma: no cover - constructed canonically
            raise RuntimeError("Permission arguments must be a JSON object.")
        return value


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """A resolved audit fact; never sufficient to revive a pending request."""

    call_id: str
    tool_name: str
    mode: PermissionMode
    resolution: PermissionResolution
    source: PermissionDecisionSource
    final_arguments_json: str
    intent: OperationIntent
    binding: str
    reason: str

    @property
    def approved(self) -> bool:
        return self.resolution == "approved"

    @property
    def final_arguments(self) -> dict[str, Any]:
        value = json.loads(self.final_arguments_json)
        if not isinstance(value, dict):  # pragma: no cover - constructed canonically
            raise RuntimeError("Permission arguments must be a JSON object.")
        return value

    def record(self) -> dict[str, object]:
        """Return a secret-free durable record without a pending request capability."""

        return {
            "version": 1,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "mode": self.mode.value,
            "resolution": self.resolution,
            "source": self.source,
            "final_arguments_sha256": hashlib.sha256(
                self.final_arguments_json.encode("utf-8")
            ).hexdigest(),
            "operation_intent": self.intent.audit_record(),
            "reason": self.reason,
        }


_FILE_READ_TOOLS = frozenset({"read", "grep", "find", "ls"})
_FILE_WRITE_TOOLS = frozenset({"write", "edit"})
_NETWORK_COMMAND = re.compile(
    r"(?:^|\s)(?:curl|wget|iwr|invoke-webrequest|ssh|scp)(?:\s|$)"
    r"|(?:^|\s)git\s+(?:clone|fetch|pull|push)(?:\s|$)"
    r"|(?:^|\s)(?:pip|pip3)\s+install(?:\s|$)",
    re.IGNORECASE,
)
_READABLE_SHELL_COMMANDS = frozenset(
    {"cat", "dir", "echo", "exit", "find", "findstr", "grep", "ls", "rg", "type"}
)


class PermissionPolicy:
    """Classify final arguments and apply the canonical Host permission matrix."""

    def __init__(self, workspace: Path, *, read_only_shell_guaranteed: bool = False) -> None:
        self._workspace = workspace.resolve()
        self._read_only_shell_guaranteed = read_only_shell_guaranteed

    def evaluate(self, mode: PermissionMode | str, call: ToolCallLike) -> PermissionEvaluation:
        try:
            normalized_mode = PermissionMode(mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Permission Mode: {mode!r}") from exc
        arguments = json_object_snapshot(call.arguments, label="final ToolCall arguments")
        arguments_json = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        intent = self._intent(call.tool_name, arguments)
        action, reason = self._matrix(normalized_mode, intent)
        binding = self.binding(call.call_id, call.tool_name, arguments, intent)
        return PermissionEvaluation(action, intent, binding, reason, arguments_json)

    @staticmethod
    def binding(
        call_id: str,
        tool_name: str,
        final_arguments: dict[str, Any],
        intent: OperationIntent,
    ) -> str:
        payload = {
            "call_id": call_id,
            "tool_name": tool_name,
            "final_arguments": final_arguments,
            "operation_intent": intent.record(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _intent(self, tool_name: str, arguments: dict[str, Any]) -> OperationIntent:
        if tool_name in _FILE_READ_TOOLS | _FILE_WRITE_TOOLS:
            target = self._normalize_target(str(arguments.get("path", ".")))
            scope = self._scope((target,))
            kind = OperationKind.READ if tool_name in _FILE_READ_TOOLS else OperationKind.WRITE
            return OperationIntent(
                tool_name,
                kind,
                scope,
                (str(target),),
                f"{kind.value} target is {scope.value}",
            )
        if tool_name == "bash":
            return self._bash_intent(arguments)
        return OperationIntent(
            tool_name,
            OperationKind.CUSTOM,
            TargetScope.WORKSPACE,
            (str(self._workspace),),
            "Host explicitly registered a custom Tool without a file target contract",
        )

    def _bash_intent(self, arguments: dict[str, Any]) -> OperationIntent:
        command = str(arguments.get("command", "")).strip()
        cwd_path = self._normalize_target(str(arguments.get("cwd", ".")))
        has_ambiguous_shell_syntax = any(marker in command for marker in ("\\", "$", "%", "!", "^"))
        if not command or has_ambiguous_shell_syntax or self._has_unquoted_shell_meta(command):
            return self._unknown_bash_intent(
                command,
                cwd_path,
                "shell command could not be classified conservatively",
            )
        if _NETWORK_COMMAND.search(command):
            return OperationIntent(
                "bash",
                OperationKind.NETWORK,
                self._scope((cwd_path,)),
                (str(cwd_path),),
                "shell command contains a recognized network operation",
                command,
                str(cwd_path),
            )
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            tokens = []
        executable = Path(tokens[0]).name.lower() if tokens else ""
        recognized = executable in _READABLE_SHELL_COMMANDS or self._recognized_shell(tokens)
        if not recognized:
            return self._unknown_bash_intent(
                command,
                cwd_path,
                "shell executable or arguments are not in the conservative classifier",
            )
        targets = [cwd_path]
        for token in tokens[1:]:
            if token.startswith("-"):
                if any(marker in token for marker in ("=", "/", "\\", "..")):
                    return self._unknown_bash_intent(
                        command,
                        cwd_path,
                        "shell option may contain a path that cannot be classified reliably",
                    )
                continue
            if "://" in token:
                continue
            if "/" in token or "\\" in token or token.startswith("."):
                targets.append(self._normalize_target(token, cwd=cwd_path))
        scope = self._scope(tuple(targets))
        return OperationIntent(
            "bash",
            OperationKind.SHELL,
            scope,
            tuple(str(target) for target in targets),
            "shell command and recognizable targets were classified conservatively",
            command,
            str(cwd_path),
        )

    @staticmethod
    def _unknown_bash_intent(command: str, cwd_path: Path, reason: str) -> OperationIntent:
        return OperationIntent(
            "bash",
            OperationKind.UNKNOWN,
            TargetScope.UNKNOWN,
            (str(cwd_path),),
            reason,
            command,
            str(cwd_path),
        )

    @staticmethod
    def _recognized_shell(tokens: list[str]) -> bool:
        if not tokens:
            return False
        executable = Path(tokens[0]).name.lower()
        if executable == "git":
            return len(tokens) > 1 and tokens[1].lower() in {"diff", "log", "show", "status"}
        return False

    @staticmethod
    def _has_unquoted_shell_meta(command: str) -> bool:
        quote: str | None = None
        escaped = False
        for index, character in enumerate(command):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote != "'":
                escaped = True
                continue
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in {"'", '"'}:
                quote = character
                continue
            if character in "|&;<>`":
                return True
            if character == "$" and index + 1 < len(command) and command[index + 1] == "(":
                return True
        return quote is not None

    def _normalize_target(self, raw: str, *, cwd: Path | None = None) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (self._workspace if cwd is None else cwd) / candidate
        return candidate.resolve(strict=False)

    def _scope(self, targets: tuple[Path, ...]) -> TargetScope:
        for target in targets:
            try:
                target.relative_to(self._workspace)
            except ValueError:
                return TargetScope.OUTSIDE
        return TargetScope.WORKSPACE

    def _matrix(
        self,
        mode: PermissionMode,
        intent: OperationIntent,
    ) -> tuple[PermissionAction, str]:
        if mode is PermissionMode.FULL:
            return PermissionAction.ALLOW, "full bypasses Kernel approval and containment"
        if intent.scope is TargetScope.OUTSIDE:
            return PermissionAction.DENY, "non-full modes contain Tool Execution to the workspace"
        if intent.kind is OperationKind.UNKNOWN or intent.scope is TargetScope.UNKNOWN:
            return PermissionAction.DENY, "unclassified operations require full mode"
        if mode is PermissionMode.PLAN:
            if intent.kind is OperationKind.READ:
                return PermissionAction.ALLOW, "plan allows contained workspace reads"
            if intent.kind is OperationKind.SHELL and self._read_only_shell_guaranteed:
                return PermissionAction.ALLOW, "environment guarantees read-only diagnostic shell"
            return PermissionAction.DENY, "plan rejects operations not guaranteed read-only"
        if mode is PermissionMode.ASK:
            if intent.kind is OperationKind.READ:
                return PermissionAction.ALLOW, "ask allows contained workspace reads"
            return PermissionAction.ASK, "ask requires one Host decision for this final ToolCall"
        if intent.kind is OperationKind.NETWORK:
            return PermissionAction.DENY, "auto rejects recognized network operations"
        return PermissionAction.ALLOW, "auto allows ordinary contained workspace operations"


def make_permission_request(
    *,
    run_id: str,
    ordinal: int,
    call: ToolCallLike,
    evaluation: PermissionEvaluation,
) -> PermissionRequest:
    request_id = f"{run_id}:permission:{ordinal}:{evaluation.binding[:16]}"
    return PermissionRequest(
        request_id,
        run_id,
        call.call_id,
        call.tool_name,
        evaluation.final_arguments_json,
        evaluation.intent,
        evaluation.binding,
        evaluation.reason,
    )


def validate_permission_decision_record(payload: dict[str, object]) -> None:
    """Validate the exact secret-free durable permission decision schema."""

    expected = {
        "version",
        "call_id",
        "tool_name",
        "mode",
        "resolution",
        "source",
        "final_arguments_sha256",
        "operation_intent",
        "reason",
    }
    if set(payload) != expected or payload.get("version") != 1:
        raise ValueError("Permission Decision has an invalid durable schema")
    for name in ("call_id", "tool_name", "reason"):
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Permission Decision field {name!r} must be a non-empty string")
    try:
        PermissionMode(payload.get("mode"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("Permission Decision has an invalid mode") from exc
    if payload.get("resolution") not in {"approved", "denied"}:
        raise ValueError("Permission Decision has an invalid resolution")
    if payload.get("source") not in {"policy", "host"}:
        raise ValueError("Permission Decision has an invalid source")
    digest = payload.get("final_arguments_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Permission Decision has an invalid arguments digest")
    raw_intent = payload.get("operation_intent")
    if not isinstance(raw_intent, dict):
        raise ValueError("Permission Decision requires an Operation Intent")
    intent_keys = {
        "tool_name",
        "kind",
        "scope",
        "targets",
        "reason",
        "cwd",
        "command_sha256",
    }
    if set(raw_intent) != intent_keys or raw_intent.get("tool_name") != payload.get("tool_name"):
        raise ValueError("Permission Decision has an invalid Operation Intent schema")
    try:
        OperationKind(raw_intent.get("kind"))  # type: ignore[arg-type]
        TargetScope(raw_intent.get("scope"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Permission Decision has an invalid Operation Intent classification"
        ) from exc
    targets = raw_intent.get("targets")
    if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
        raise ValueError("Permission Decision Operation Intent targets must be strings")
    if not isinstance(raw_intent.get("reason"), str):
        raise ValueError("Permission Decision Operation Intent requires a reason")
    for optional in ("cwd", "command_sha256"):
        value = raw_intent.get(optional)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Permission Decision Operation Intent {optional} is invalid")
    command_digest = raw_intent.get("command_sha256")
    if command_digest is not None and re.fullmatch(r"[0-9a-f]{64}", command_digest) is None:
        raise ValueError("Permission Decision has an invalid command digest")


def make_permission_decision(
    *,
    mode: PermissionMode,
    call: ToolCallLike,
    evaluation: PermissionEvaluation,
    approved: bool,
    source: PermissionDecisionSource,
    reason: str | None = None,
) -> PermissionDecision:
    return PermissionDecision(
        call.call_id,
        call.tool_name,
        mode,
        "approved" if approved else "denied",
        source,
        evaluation.final_arguments_json,
        evaluation.intent,
        evaluation.binding,
        evaluation.reason if reason is None else reason,
    )
