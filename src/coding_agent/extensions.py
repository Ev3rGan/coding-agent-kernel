"""Explicit Extension contract: registration, fixed Hooks, and deterministic composition.

An Extension is an explicitly constructed Python object. It registers Tools,
custom SessionEntry kinds, and fixed Hook handlers through the ExtensionRuntime
seam. Handlers compose in deterministic registration order and every accepted
outcome is validated. An Extension never receives an arbitrary writable Kernel
handle; all mutation crosses a fixed, re-validated seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias

from coding_agent.context import ModelContext
from coding_agent.events import AssistantMessage, ToolCall, ToolResult
from coding_agent.provider import ProviderRequest
from coding_agent.tools import Tool


class ExtensionEventKind(StrEnum):
    """The independent Extension dispatch contract, kept separate from public stream."""

    REGISTERED = "registered"
    HOOK_START = "hook_start"
    HOOK_END = "hook_end"
    HOOK_TRANSFORMED = "hook_transformed"
    HOOK_BLOCKED = "hook_blocked"
    HOOK_SUPPLEMENTED = "hook_supplemented"
    HOOK_ERROR = "hook_error"


@dataclass(frozen=True, slots=True)
class ExtensionEvent:
    """One deterministic Extension observation or interception outcome."""

    kind: ExtensionEventKind
    hook: str
    extension: str
    message: str = ""


class HookName(StrEnum):
    """The fixed set of Kernel Hooks an Extension may observe or intercept."""

    BEFORE_AGENT_START = "before_agent_start"
    INPUT = "input"
    CONTEXT = "context"
    PROVIDER_REQUEST = "provider_request"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_EXECUTION = "tool_execution"
    MESSAGE = "message"
    AGENT_SETTLED = "agent_settled"


@dataclass(frozen=True, slots=True)
class ContextSupplement:
    """An additive, validated set of context lines an Extension may contribute."""

    lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(line, str) for line in self.lines):
            raise ValueError("context supplement lines must be strings")
        if any("\n" in line for line in self.lines):
            raise ValueError("context supplement lines must not contain newlines")


@dataclass(frozen=True, slots=True)
class ToolResultSupplement:
    """A model-visible note an Extension may attach to a ToolResult."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("tool result supplement must be a string")


@dataclass(frozen=True, slots=True)
class HookInput:
    """Immutable snapshot delivered to an Extension handler for one Hook."""

    hook: HookName
    run_id: str | None = None
    turn_id: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    context: ModelContext | None = None
    request: ProviderRequest | None = None
    message: AssistantMessage | None = None


@dataclass(frozen=True, slots=True)
class HookResult:
    """The validated outcome of one Extension handler invocation."""

    outcome: Literal["observe", "transform", "block", "supplement"]
    tool_call: ToolCall | None = None
    context_supplement: ContextSupplement | None = None
    tool_result_supplement: ToolResultSupplement | None = None
    block_reason: str = ""

    @classmethod
    def observe(cls) -> HookResult:
        return cls("observe")

    @classmethod
    def transform_tool_call(cls, tool_call: ToolCall) -> HookResult:
        return cls("transform", tool_call=tool_call)

    @classmethod
    def block(cls, reason: str) -> HookResult:
        return cls("block", block_reason=reason)

    @classmethod
    def supplement_context(cls, lines: ContextSupplement) -> HookResult:
        return cls("supplement", context_supplement=lines)

    @classmethod
    def supplement_tool_result(cls, note: ToolResultSupplement) -> HookResult:
        return cls("supplement", tool_result_supplement=note)


HookHandler: TypeAlias = Callable[[HookInput], HookResult | Awaitable[HookResult]]


class ExtensionError(ValueError):
    """The fixed Kernel response to an illegal Extension action or handler failure."""

    code = "extension_error"


@dataclass(frozen=True, slots=True)
class DeclaredTool:
    """A Tool an Extension contributed, together with its owning Extension."""

    extension: str
    tool: Tool
    enabled: bool


class ExtensionRuntime:
    """Deterministic, ordered composition of Extensions against fixed Hooks.

    Registration order is authoritative: handlers run in the order registered.
    Every accepted transform is re-validated by the Kernel seam. A handler that
    raises or returns an illegal mutation becomes a structured ExtensionEvent and
    a normalized rejection, never arbitrary Kernel internals access.
    """

    def __init__(self) -> None:
        self._extensions: list[str] = []
        self._handlers: dict[HookName, list[tuple[int, str, HookHandler]]] = {
            hook: [] for hook in HookName
        }
        self._event_log: list[ExtensionEvent] = []
        self._tools: list[DeclaredTool] = []
        self._session_entry_kinds: set[str] = set()
        self._sequence = 0

    @property
    def events(self) -> tuple[ExtensionEvent, ...]:
        """Return every dispatched Extension observation exactly once."""
        return tuple(self._event_log)

    @property
    def registered_extensions(self) -> tuple[str, ...]:
        return tuple(self._extensions)

    @property
    def session_entry_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._session_entry_kinds))

    def has_handler(self, hook: HookName) -> bool:
        """Whether any registered Extension registered a handler for this Hook."""
        return bool(self._handlers[hook])

    @property
    def declared_tools(self) -> tuple[DeclaredTool, ...]:
        """Return every Tool every Extension contributed, in registration order."""
        return tuple(self._tools)

    def declared_tools_of(self, extension: str) -> tuple[DeclaredTool, ...]:
        return tuple(item for item in self._tools if item.extension == extension)

    def register(self, extension: Extension) -> None:
        """Register one Extension, calling its register() through the seam."""
        if not isinstance(extension.name, str) or not extension.name:
            raise ExtensionError("extension must expose a non-empty name")
        if extension.name in self._extensions:
            raise ExtensionError(f"extension already registered: {extension.name}")
        self._extensions.append(extension.name)
        extension.register(self)
        self._event_log.append(
            ExtensionEvent(ExtensionEventKind.REGISTERED, "__register__", extension.name)
        )

    def on(self, extension: str, hook: HookName, handler: HookHandler) -> None:
        """Register one handler; this is the seam an Extension calls internally."""
        self._handlers[hook].append((self._sequence, extension, handler))
        self._sequence += 1

    def declare_tool(self, extension: str, tool: Tool, *, enabled: bool = False) -> None:
        """Receive a Tool contributed by an Extension for the Host to enable."""
        self._tools.append(DeclaredTool(extension, tool, enabled))

    def declare_session_entry_kind(self, kind: str) -> None:
        """Register a custom SessionEntry kind owned by one Extension."""
        if not isinstance(kind, str) or not kind:
            raise ExtensionError("SessionEntry kind must be a non-empty string")
        self._session_entry_kinds.add(kind)

    async def run_once(
        self, hook: HookName, input_: HookInput
    ) -> tuple[dict[str, object], tuple[ExtensionEvent, ...]]:
        """Compose every handler for one Hook in deterministic registration order.

        Returns a decision mapping accepted outcomes, plus the events produced.
        The first rejecting handler short-circuits remaining handlers for that
        Hook, mirroring fixed failure semantics.
        """
        decision: dict[str, object] = {"hook": hook.value}
        emitted: list[ExtensionEvent] = []
        current = input_
        for order, extension, handler in self._handlers[hook]:
            start = ExtensionEvent(
                ExtensionEventKind.HOOK_START, hook.value, extension, str(order)
            )
            emitted.append(start)
            self._event_log.append(start)
            try:
                raw = handler(current)
                result = await raw if isinstance(raw, Awaitable) else raw
                accepted, applied = self._apply_outcome(
                    hook, extension, result, decision, input_
                )
                # transform 顺序合成：后续 handler 看到前序已接受的 tool_call，实现真正的链式合成。
                if accepted and hook is HookName.TOOL_CALL:
                    transformed = decision.get("tool_call")
                    if isinstance(transformed, ToolCall):
                        current = replace(current, tool_call=transformed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                rejection = ExtensionError(f"{type(exc).__name__}: {exc}")
                decision.clear()
                decision.update(
                    {"hook": hook.value, "rejected": True, "reason": str(rejection)}
                )
                error_event = ExtensionEvent(
                    ExtensionEventKind.HOOK_ERROR, hook.value, extension, str(rejection)
                )
                emitted.append(error_event)
                self._event_log.append(error_event)
                break
            emitted.append(applied)
            self._event_log.append(applied)
            if not accepted:
                break
        return decision, tuple(emitted)

    def _apply_outcome(
        self,
        hook: HookName,
        extension: str,
        result: HookResult,
        decision: dict[str, object],
        input_: HookInput,
    ) -> tuple[bool, ExtensionEvent]:
        if not isinstance(result, HookResult):
            raise ExtensionError(
                f"handler for {hook.value} must return a HookResult, "
                f"got {type(result).__name__}"
            )
        if result.outcome == "observe":
            return True, ExtensionEvent(
                ExtensionEventKind.HOOK_END, hook.value, extension, "observe"
            )
        if result.outcome == "block":
            if not result.block_reason:
                raise ExtensionError("block() requires a non-empty reason")
            decision["blocked"] = True
            decision["reason"] = result.block_reason
            return False, ExtensionEvent(
                ExtensionEventKind.HOOK_BLOCKED,
                hook.value,
                extension,
                result.block_reason,
            )
        if result.outcome == "transform":
            if hook is not HookName.TOOL_CALL or result.tool_call is None:
                raise ExtensionError(
                    "transform is only valid for the tool_call Hook, on a ToolCall"
                )
            if input_.tool_call is None:
                raise ExtensionError("tool_call transform requires a ToolCall input")
            call = result.tool_call
            if call.call_id != input_.tool_call.call_id:
                raise ExtensionError(
                    f"{extension} must preserve the original call_id in a ToolCall transform"
                )
            if not call.call_id or not call.tool_name or type(call.arguments) is not dict:
                raise ExtensionError(f"{extension} produced an invalid ToolCall transform")
            decision["tool_call"] = call
            return True, ExtensionEvent(
                ExtensionEventKind.HOOK_TRANSFORMED,
                hook.value,
                extension,
                call.tool_name,
            )
        if hook is HookName.CONTEXT and result.context_supplement is not None:
            existing = decision.get("context_lines")
            merged = tuple(existing) if isinstance(existing, tuple) else ()
            decision["context_lines"] = (*merged, *result.context_supplement.lines)
            return True, ExtensionEvent(
                ExtensionEventKind.HOOK_SUPPLEMENTED,
                hook.value,
                extension,
                ",".join(result.context_supplement.lines),
            )
        if hook is HookName.TOOL_RESULT and result.tool_result_supplement is not None:
            decision["tool_result_supplement"] = result.tool_result_supplement.text
            return True, ExtensionEvent(
                ExtensionEventKind.HOOK_SUPPLEMENTED,
                hook.value,
                extension,
                result.tool_result_supplement.text,
            )
        raise ExtensionError(
            f"supplement outcome does not match hook {hook.value} for {extension}"
        )


class Extension(Protocol):
    """The public Extension contract implemented by an explicit Python object.

    The Kernel calls register() before an Agent Run starts. An Extension receives
    only the ExtensionRuntime seam, through which it registers Tools, SessionEntry
    kinds, and fixed Hook handlers. It never receives the Kernel or Session.
    """

    @property
    def name(self) -> str: ...

    def register(self, runtime: ExtensionRuntime) -> None: ...
