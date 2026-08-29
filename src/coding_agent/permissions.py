"""Host-owned Permission Mode, Operation Intent, and one-time decision values."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
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


class PermissionClassificationError(ValueError):
    """A permission-classification failure whose reason is safe for the model."""


def permission_classification_error_message(exc: Exception) -> str:
    """Return a model-safe message without exposing unexpected Host failures."""

    message = "Permission classification failed for final ToolCall"
    if isinstance(exc, PermissionClassificationError):
        return f"{message}: {exc}"
    return message


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


def _decode_arguments_object(arguments_json: str) -> dict[str, Any]:
    value = json.loads(arguments_json)
    if not isinstance(value, dict):  # pragma: no cover - constructed canonically
        raise RuntimeError("Permission arguments must be a JSON object.")
    return value


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
        return _decode_arguments_object(self.final_arguments_json)


PermissionResolution = Literal["approved", "denied"]
PermissionDecisionSource = Literal["policy", "host"]


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A transient one-time request bound to one final ToolCall and Operation Intent."""

    request_id: str
    run_id: str
    call_id: str
    tool_name: str
    mode: PermissionMode
    final_arguments_json: str
    intent: OperationIntent
    binding: str
    reason: str

    @property
    def final_arguments(self) -> dict[str, Any]:
        return _decode_arguments_object(self.final_arguments_json)


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
        return _decode_arguments_object(self.final_arguments_json)

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
_CHILD_PROCESS_SHELL_OPTIONS = frozenset(
    {"--hostname-bin", "--pre", "-exec", "-execdir", "-ok", "-okdir"}
)


def _canonical_permission_actions(
    mode: PermissionMode,
    kind: OperationKind,
    scope: TargetScope,
    *,
    read_only_shell_guaranteed: bool | None,
) -> frozenset[PermissionAction]:
    """Return canonical actions; ``None`` preserves either valid PLAN shell outcome."""

    if mode is PermissionMode.FULL:
        return frozenset({PermissionAction.ALLOW})
    if mode is PermissionMode.PLAN:
        if kind is OperationKind.READ and scope is TargetScope.WORKSPACE:
            return frozenset({PermissionAction.ALLOW})
        if kind is OperationKind.SHELL and scope is TargetScope.WORKSPACE:
            if read_only_shell_guaranteed is None:
                return frozenset({PermissionAction.ALLOW, PermissionAction.DENY})
            if read_only_shell_guaranteed:
                return frozenset({PermissionAction.ALLOW})
        return frozenset({PermissionAction.DENY})
    if mode is PermissionMode.ASK:
        if kind is OperationKind.READ and scope is TargetScope.WORKSPACE:
            return frozenset({PermissionAction.ALLOW})
        return frozenset({PermissionAction.ASK})
    if kind in {
        OperationKind.NETWORK,
        OperationKind.CUSTOM,
        OperationKind.UNKNOWN,
    } or scope in {TargetScope.OUTSIDE, TargetScope.UNKNOWN}:
        return frozenset({PermissionAction.ASK})
    return frozenset({PermissionAction.ALLOW})


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
            TargetScope.UNKNOWN,
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
        executable = tokens[0].lower() if tokens else ""
        recognized = executable in _READABLE_SHELL_COMMANDS
        if not recognized:
            return self._unknown_bash_intent(
                command,
                cwd_path,
                "shell executable or arguments are not in the conservative classifier",
            )
        targets = [cwd_path]
        for token in tokens[1:]:
            if token.startswith("-"):
                if token in _CHILD_PROCESS_SHELL_OPTIONS or any(
                    marker in token for marker in ("=", "/", "\\", "..")
                ):
                    return self._unknown_bash_intent(
                        command,
                        cwd_path,
                        "shell option may contain a path that cannot be classified reliably",
                    )
                continue
            if "://" in token:
                continue
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
    def _has_unquoted_shell_meta(command: str) -> bool:
        quote: str | None = None
        quote_characters = {'"'} if os.name == "nt" else {"'", '"'}
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
            if character in quote_characters:
                quote = character
                continue
            if character in "|&;<>`*?[]\r\n":
                return True
            if character == "$" and index + 1 < len(command) and command[index + 1] == "(":
                return True
        return quote is not None

    def _normalize_target(self, raw: str, *, cwd: Path | None = None) -> Path:
        if "\0" in raw:
            raise PermissionClassificationError("path target contains NUL")
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
        actions = _canonical_permission_actions(
            mode,
            intent.kind,
            intent.scope,
            read_only_shell_guaranteed=self._read_only_shell_guaranteed,
        )
        action = next(iter(actions))
        reasons = {
            (PermissionMode.FULL, PermissionAction.ALLOW): (
                "full bypasses Kernel approval and containment"
            ),
            (PermissionMode.PLAN, PermissionAction.ALLOW): (
                "plan allows operations guaranteed read-only"
            ),
            (PermissionMode.PLAN, PermissionAction.DENY): (
                "plan rejects operations not guaranteed read-only"
            ),
            (PermissionMode.ASK, PermissionAction.ALLOW): ("ask allows contained workspace reads"),
            (PermissionMode.ASK, PermissionAction.ASK): (
                "ask requires one Host decision for this final ToolCall"
            ),
            (PermissionMode.AUTO, PermissionAction.ASK): (
                "auto requires one Host decision for elevated authority"
            ),
            (PermissionMode.AUTO, PermissionAction.ALLOW): (
                "auto allows ordinary contained workspace operations"
            ),
        }
        return action, reasons[(mode, action)]


def make_permission_request(
    *,
    run_id: str,
    ordinal: int,
    mode: PermissionMode,
    call: ToolCallLike,
    evaluation: PermissionEvaluation,
) -> PermissionRequest:
    request_id = f"{run_id}:permission:{ordinal}:{evaluation.binding[:16]}:{secrets.token_hex(16)}"
    return PermissionRequest(
        request_id,
        run_id,
        call.call_id,
        call.tool_name,
        mode,
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
        mode = PermissionMode(payload.get("mode"))  # type: ignore[arg-type]
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
        intent_kind = OperationKind(raw_intent.get("kind"))  # type: ignore[arg-type]
        intent_scope = TargetScope(raw_intent.get("scope"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Permission Decision has an invalid Operation Intent classification"
        ) from exc
    targets = raw_intent.get("targets")
    if (
        not isinstance(targets, list)
        or not targets
        or not all(isinstance(target, str) and target for target in targets)
    ):
        raise ValueError("Permission Decision Operation Intent targets must be non-empty strings")
    intent_reason = raw_intent.get("reason")
    if not isinstance(intent_reason, str) or not intent_reason:
        raise ValueError("Permission Decision Operation Intent requires a non-empty reason")
    for optional in ("cwd", "command_sha256"):
        value = raw_intent.get(optional)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Permission Decision Operation Intent {optional} is invalid")
    command_digest = raw_intent.get("command_sha256")
    if command_digest is not None and re.fullmatch(r"[0-9a-f]{64}", command_digest) is None:
        raise ValueError("Permission Decision has an invalid command digest")
    cwd = raw_intent.get("cwd")
    if payload.get("tool_name") == "bash":
        if (
            intent_kind not in {OperationKind.SHELL, OperationKind.NETWORK, OperationKind.UNKNOWN}
            or not isinstance(cwd, str)
            or not cwd
            or not isinstance(command_digest, str)
        ):
            raise ValueError("Permission Decision has an inconsistent bash Operation Intent")
    elif cwd is not None or command_digest is not None:
        raise ValueError("Permission Decision has shell fields on a non-bash Operation Intent")
    tool_name = payload["tool_name"]
    if tool_name in _FILE_READ_TOOLS and intent_kind is not OperationKind.READ:
        raise ValueError("Permission Decision has a non-read intent for a read Tool")
    if tool_name in _FILE_WRITE_TOOLS and intent_kind is not OperationKind.WRITE:
        raise ValueError("Permission Decision has a non-write intent for a write Tool")
    if tool_name in _FILE_READ_TOOLS | _FILE_WRITE_TOOLS:
        if intent_scope is TargetScope.UNKNOWN:
            raise ValueError("Permission Decision has an unknown scope for a file Tool")
    elif tool_name != "bash" and (
        intent_kind is not OperationKind.CUSTOM or intent_scope is not TargetScope.UNKNOWN
    ):
        raise ValueError("Permission Decision has an inconsistent custom Tool intent")

    record_action = (
        PermissionAction.ASK
        if payload["source"] == "host"
        else (
            PermissionAction.ALLOW if payload["resolution"] == "approved" else PermissionAction.DENY
        )
    )
    valid_actions = _canonical_permission_actions(
        mode,
        intent_kind,
        intent_scope,
        read_only_shell_guaranteed=None,
    )
    if record_action not in valid_actions:
        raise ValueError("Permission Decision contradicts the canonical mode matrix")


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


def make_permission_request_decision(
    request: PermissionRequest,
    *,
    approved: bool,
    reason: str,
) -> PermissionDecision:
    """Resolve a transient request as a non-replayable Host decision."""

    return PermissionDecision(
        request.call_id,
        request.tool_name,
        request.mode,
        "approved" if approved else "denied",
        "host",
        request.final_arguments_json,
        request.intent,
        request.binding,
        reason,
    )
