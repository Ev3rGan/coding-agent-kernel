"""Fixed, explicit Extension registration and dispatch contracts."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from types import MappingProxyType
from typing import Any, Generic, Literal, NoReturn, Protocol, TypeAlias, TypeVar, cast

from coding_agent.callout import dispose_awaitable, invoke_sync_callout
from coding_agent.context import (
    CompactionPlan,
    ContextHookInput,
    ModelContext,
    estimate_provider_request_characters,
)
from coding_agent.events import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    ProviderStreamEvent,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    TokenUsage,
    ToolCall,
    ToolError,
    ToolResult,
    assistant_message_record,
    validate_provider_stream_event,
)
from coding_agent.json_contract import json_object_snapshot
from coding_agent.provider import (
    BranchSummaryMessage,
    ModelMessage,
    ModelProvider,
    ProviderRequest,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.session import SessionEntry
from coding_agent.tools import Tool


class Hook(StrEnum):
    """The complete, closed set of Kernel Extension interception seams."""

    INPUT = "input"
    BEFORE_AGENT_START = "before_agent_start"
    CONTEXT = "context"
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_RESPONSE = "provider_response"
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_UPDATE = "tool_execution_update"
    TOOL_EXECUTION_END = "tool_execution_end"
    SESSION_CONFIGURATION = "session_configuration"
    SESSION_ENTRY = "session_entry"
    SESSION_RESUMED = "session_resumed"
    SESSION_TREE = "session_tree"
    COMPACTION_START = "compaction_start"
    COMPACTION_END = "compaction_end"
    COMPACTION_FAILED = "compaction_failed"
    AGENT_SETTLED = "agent_settled"


OutcomeName: TypeAlias = Literal["observe", "transform", "block", "supplement"]


@dataclass(frozen=True, slots=True)
class HookPolicy:
    """Public, fixed result algebra allowed at one Hook."""

    outcomes: tuple[OutcomeName, ...]


_OBSERVE = HookPolicy(("observe",))
_HOOK_POLICIES = MappingProxyType(
    {
        Hook.INPUT: HookPolicy(("observe", "transform", "block")),
        Hook.BEFORE_AGENT_START: HookPolicy(("observe", "block")),
        Hook.CONTEXT: HookPolicy(("observe", "transform", "block", "supplement")),
        Hook.PROVIDER_REQUEST: HookPolicy(("observe", "transform", "block", "supplement")),
        Hook.PROVIDER_RESPONSE: HookPolicy(("observe", "transform", "block")),
        Hook.AGENT_START: _OBSERVE,
        Hook.AGENT_END: _OBSERVE,
        Hook.TURN_START: _OBSERVE,
        Hook.TURN_END: _OBSERVE,
        Hook.MESSAGE_START: _OBSERVE,
        Hook.MESSAGE_UPDATE: _OBSERVE,
        Hook.MESSAGE_END: HookPolicy(("observe", "transform", "block")),
        Hook.TOOL_CALL: HookPolicy(("observe", "transform", "block")),
        Hook.TOOL_RESULT: HookPolicy(("observe", "transform", "block", "supplement")),
        Hook.TOOL_EXECUTION_START: _OBSERVE,
        Hook.TOOL_EXECUTION_UPDATE: _OBSERVE,
        Hook.TOOL_EXECUTION_END: _OBSERVE,
        Hook.SESSION_CONFIGURATION: _OBSERVE,
        Hook.SESSION_ENTRY: _OBSERVE,
        Hook.SESSION_RESUMED: _OBSERVE,
        Hook.SESSION_TREE: _OBSERVE,
        Hook.COMPACTION_START: HookPolicy(("observe", "transform", "block")),
        Hook.COMPACTION_END: _OBSERVE,
        Hook.COMPACTION_FAILED: _OBSERVE,
        Hook.AGENT_SETTLED: HookPolicy(("observe", "supplement")),
    }
)


def hook_policy(hook: Hook) -> HookPolicy:
    """Return the immutable allowed-outcome policy for one fixed Hook."""

    return _HOOK_POLICIES[hook]


class ExtensionEventKind(StrEnum):
    EXTENSION_REGISTERED = "extension_registered"
    CAPABILITY_REGISTERED = "capability_registered"
    DISPATCH_STARTED = "dispatch_started"
    HANDLER_OUTCOME = "handler_outcome"
    DISPATCH_COMPLETED = "dispatch_completed"
    DISPATCH_BLOCKED = "dispatch_blocked"
    OUTCOME_REJECTED = "outcome_rejected"
    HANDLER_FAILED = "handler_failed"
    RUNTIME_FAILURE = "runtime_failure"


@dataclass(frozen=True, slots=True)
class ExtensionEvent:
    """One independently consumed Extension registration or dispatch fact."""

    sequence: int
    kind: ExtensionEventKind
    extension_name: str | None = None
    hook: Hook | None = None
    capability: str | None = None
    outcome: str | None = None
    revalidated: bool = False
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class InputHookInput:
    prompt: str


@dataclass(frozen=True, slots=True)
class ToolCallHookInput:
    """Immutable ToolCall snapshot; decoded arguments are returned as a fresh value."""

    run_id: str
    turn_id: str
    call_id: str
    tool_name: str
    arguments_json: str

    @property
    def arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments_json)
        if not isinstance(value, dict):  # pragma: no cover - constructors validate this
            raise ExtensionDispatchError("ToolCall arguments snapshot is not an object")
        return value


@dataclass(frozen=True, slots=True)
class ContextSupplement:
    """Additional canonical Context material, never a second Context builder."""

    project_context: tuple[str, ...] = ()
    messages: tuple[ModelMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderRequestSupplement:
    project_context: tuple[str, ...] = ()
    messages: tuple[ModelMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderRequestHookInput:
    request: ProviderRequest


@dataclass(frozen=True, slots=True)
class ProviderResponseHookInput:
    request: ProviderRequest
    event: ProviderStreamEvent
    attempt: int


@dataclass(frozen=True, slots=True)
class MessageHookInput:
    run_id: str
    turn_id: str
    message_id: str
    message: AssistantMessage
    provider_event: ProviderStreamEvent | None = None


@dataclass(frozen=True, slots=True)
class ToolResultHookInput:
    run_id: str
    turn_id: str
    call_id: str
    tool_name: str
    status: str
    output_json: str | None
    error_code: str | None
    error_message: str | None

    @property
    def result(self) -> ToolResult:
        output = None if self.output_json is None else json.loads(self.output_json)
        error = (
            None
            if self.error_code is None or self.error_message is None
            else ToolError(self.error_code, self.error_message)
        )
        return ToolResult(
            self.call_id,
            self.tool_name,
            self.status,  # type: ignore[arg-type]
            output,
            error,
        )


@dataclass(frozen=True, slots=True)
class ToolResultSupplement:
    output: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BeforeAgentStartHookInput:
    run_id: str
    prompt: str
    session_id: str | None


@dataclass(frozen=True, slots=True)
class LifecycleHookInput:
    run_id: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionHookInput:
    run_id: str
    turn_id: str
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    progress_stream: str | None = None
    progress_data: str | None = None
    batch_mode: str | None = None


@dataclass(frozen=True, slots=True)
class SessionHookInput:
    run_id: str | None
    session_id: str
    entry: SessionEntry | None = None
    active_branch: tuple[str, ...] | None = None
    configuration_json: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionHookInput:
    run_id: str | None
    session_id: str
    active_branch: tuple[str, ...]
    plan: CompactionPlan | None = None
    entry: SessionEntry | None = None
    error: AgentError | None = None


@dataclass(frozen=True, slots=True)
class AgentSettledHookInput:
    result: AgentRunResult


@dataclass(frozen=True, slots=True)
class SessionEntryDraft:
    kind: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Observe:
    """Observe a Hook without changing Kernel-owned state."""


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Transform(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class Supplement(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class Block:
    code: str
    message: str


HookOutcome: TypeAlias = Observe | Transform[Any] | Supplement[Any] | Block
HookHandler: TypeAlias = Callable[[Any], HookOutcome]
SessionEntryValidator: TypeAlias = Callable[[Mapping[str, object]], None]


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value):
        return True
    if not callable(value):
        return False
    call = type(value).__call__
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)


def _is_coroutine_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value):
        return True
    if not callable(value):
        return False
    return inspect.iscoroutinefunction(type(value).__call__)


class Extension(Protocol):
    name: str

    def register(self, registry: ExtensionRegistry) -> None: ...


class ExtensionError(ValueError):
    """Base class for deterministic Extension failures."""

    code = "extension_error"


class ExtensionRegistrationError(ExtensionError):
    code = "extension_registration_error"


class ExtensionDispatchError(ExtensionError):
    code = "extension_dispatch_error"


class ExtensionBlockedError(ExtensionDispatchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _RegisteredHandler:
    extension_name: str
    handler: HookHandler


class ExtensionRegistry:
    """Capability-only registrar scoped to one Extension instance."""

    def __init__(
        self,
        extension_name: str,
        handlers: dict[Hook, list[_RegisteredHandler]],
        tools: dict[str, Tool],
        providers: dict[str, ModelProvider],
        session_entry_types: dict[str, SessionEntryValidator],
        emit: Callable[..., None],
    ) -> None:
        self._extension_name = extension_name
        self._handlers = handlers
        self._tools = tools
        self._providers = providers
        self._session_entry_types = session_entry_types
        self._emit = emit
        self._open = True

    def register_hook(self, hook: Hook, handler: HookHandler) -> None:
        self._require_open()
        if not isinstance(hook, Hook):
            raise ExtensionRegistrationError("hook must be a member of the fixed Hook set")
        if not callable(handler):
            raise ExtensionRegistrationError("hook handler must be callable")
        if _is_async_callable(handler):
            raise ExtensionRegistrationError("hook handler must be synchronous")
        self._handlers[hook].append(_RegisteredHandler(self._extension_name, handler))
        self._emit(
            ExtensionEventKind.CAPABILITY_REGISTERED,
            extension_name=self._extension_name,
            hook=hook,
            capability="hook",
        )

    def register_tool(self, tool: Tool) -> None:
        self._require_open()
        spec = getattr(tool, "spec", None)
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not name:
            raise ExtensionRegistrationError("Tool.spec.name must be a non-empty string")
        if not callable(getattr(tool, "execute", None)):
            raise ExtensionRegistrationError(f"Tool {name!r} must define async execute")
        if name in self._tools:
            raise ExtensionRegistrationError(f"Tool already registered by an Extension: {name}")
        self._tools[name] = tool
        self._emit(
            ExtensionEventKind.CAPABILITY_REGISTERED,
            extension_name=self._extension_name,
            capability=f"tool:{name}",
        )

    def register_provider(self, name: str, provider: ModelProvider) -> None:
        self._require_open()
        if not isinstance(name, str) or not name:
            raise ExtensionRegistrationError("Provider names must be non-empty strings")
        stream = getattr(provider, "stream", None)
        if not callable(stream):
            raise ExtensionRegistrationError(
                f"Provider {name!r} must satisfy the ModelProvider contract"
            )
        if _is_coroutine_callable(stream):
            raise ExtensionRegistrationError(
                f"Provider {name!r} stream must return an async iterator, not a coroutine"
            )
        if name in self._providers:
            raise ExtensionRegistrationError(f"Provider already registered: {name}")
        self._providers[name] = provider
        self._emit(
            ExtensionEventKind.CAPABILITY_REGISTERED,
            extension_name=self._extension_name,
            capability=f"provider:{name}",
        )

    def register_session_entry_type(
        self,
        name: str,
        validator: SessionEntryValidator,
    ) -> None:
        self._require_open()
        if not isinstance(name, str) or not name:
            raise ExtensionRegistrationError("SessionEntry type names must be non-empty strings")
        if name in {"configuration", "message", "compaction"}:
            raise ExtensionRegistrationError(f"SessionEntry type is reserved: {name}")
        if not callable(validator):
            raise ExtensionRegistrationError(
                f"SessionEntry type {name!r} validator must be callable"
            )
        if _is_async_callable(validator):
            raise ExtensionRegistrationError(
                f"SessionEntry type {name!r} validator must be synchronous"
            )
        if name in self._session_entry_types:
            raise ExtensionRegistrationError(f"SessionEntry type already registered: {name}")
        self._session_entry_types[name] = validator
        self._emit(
            ExtensionEventKind.CAPABILITY_REGISTERED,
            extension_name=self._extension_name,
            capability=f"session_entry:{name}",
        )

    def _require_open(self) -> None:
        if not self._open:
            raise ExtensionRegistrationError("Extension registration window is closed")

    def _close(self) -> None:
        self._open = False


class ExtensionRuntime:
    """Deep module that owns deterministic registration, dispatch, and events."""

    def __init__(self, extensions: Iterable[Extension] = ()) -> None:
        self._sequence = count(1)
        self._events: list[ExtensionEvent] = []
        staged_handlers: dict[Hook, list[_RegisteredHandler]] = {hook: [] for hook in Hook}
        staged_tools: dict[str, Tool] = {}
        staged_providers: dict[str, ModelProvider] = {}
        staged_session_entry_types: dict[str, SessionEntryValidator] = {}
        names: set[str] = set()
        for extension in tuple(extensions):
            name = getattr(extension, "name", None)
            if not isinstance(name, str) or not name.strip():
                raise ExtensionRegistrationError("Extension.name must be a non-empty string")
            if name in names:
                raise ExtensionRegistrationError(f"Extension already registered: {name}")
            register = getattr(extension, "register", None)
            if not callable(register):
                raise ExtensionRegistrationError(
                    f"Extension {name!r} must define register(registry)"
                )
            if _is_async_callable(register):
                raise ExtensionRegistrationError(f"Extension {name!r} register must be synchronous")
            registry = ExtensionRegistry(
                name,
                staged_handlers,
                staged_tools,
                staged_providers,
                staged_session_entry_types,
                self._emit,
            )
            try:
                registration_result = invoke_sync_callout(
                    cast(Callable[[ExtensionRegistry], object], register),
                    registry,
                )
                if inspect.isawaitable(registration_result):
                    dispose_awaitable(registration_result)
                    raise ExtensionRegistrationError(
                        f"Extension {name!r} register must be synchronous"
                    )
                if registration_result is not None:
                    raise ExtensionRegistrationError(
                        f"Extension {name!r} register must return None"
                    )
            except ExtensionRegistrationError:
                raise
            except asyncio.CancelledError as exc:
                raise ExtensionRegistrationError(
                    f"Extension {name!r} registration attempted cancellation"
                ) from exc
            except Exception as exc:
                raise ExtensionRegistrationError(
                    f"Extension {name!r} registration failed: {type(exc).__name__}: {exc}"
                ) from exc
            finally:
                registry._close()
            names.add(name)
            self._emit(ExtensionEventKind.EXTENSION_REGISTERED, extension_name=name)
        self._handlers = {hook: tuple(items) for hook, items in staged_handlers.items()}
        self._tools = tuple(staged_tools.values())
        self._providers = dict(staged_providers)
        self._session_entry_types = dict(staged_session_entry_types)

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def resolve_provider(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ExtensionRegistrationError(f"Unknown registered Provider: {name}") from exc

    @property
    def session_entry_types(self) -> Mapping[str, SessionEntryValidator]:
        return dict(self._session_entry_types)

    def transform_input(self, prompt: str) -> str:
        self._validate_prompt(prompt)
        value = prompt
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.INPUT)
        for registered in self._handlers[Hook.INPUT]:
            try:
                outcome = self._invoke_handler(registered, InputHookInput(value))
            except Exception as exc:
                self._emit(
                    ExtensionEventKind.HANDLER_FAILED,
                    extension_name=registered.extension_name,
                    hook=Hook.INPUT,
                    code="handler_exception",
                    message=f"{type(exc).__name__}: {exc}",
                )
                raise ExtensionDispatchError(
                    f"Extension {registered.extension_name!r} input handler failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if isinstance(outcome, Observe):
                self._emit(
                    ExtensionEventKind.HANDLER_OUTCOME,
                    extension_name=registered.extension_name,
                    hook=Hook.INPUT,
                    outcome="observe",
                )
                continue
            if isinstance(outcome, Transform):
                try:
                    self._validate_prompt(outcome.value)
                except ExtensionDispatchError as exc:
                    self._emit(
                        ExtensionEventKind.OUTCOME_REJECTED,
                        extension_name=registered.extension_name,
                        hook=Hook.INPUT,
                        outcome="transform",
                        code=exc.code,
                        message=str(exc),
                    )
                    raise
                value = outcome.value
                self._emit(
                    ExtensionEventKind.HANDLER_OUTCOME,
                    extension_name=registered.extension_name,
                    hook=Hook.INPUT,
                    outcome="transform",
                    revalidated=True,
                )
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.INPUT, outcome)
            self._emit(
                ExtensionEventKind.OUTCOME_REJECTED,
                extension_name=registered.extension_name,
                hook=Hook.INPUT,
                code="outcome_not_allowed",
                message="input Hook permits only observe, transform, or block",
            )
            raise ExtensionDispatchError(
                "input Hook permits only Observe, Transform, or Block outcomes"
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.INPUT)
        return value

    def transform_tool_call(
        self,
        call: ToolCall,
        *,
        run_id: str,
        turn_id: str,
        validator: Callable[[ToolCall], None],
    ) -> ToolCall:
        value = call
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.TOOL_CALL)
        for registered in self._handlers[Hook.TOOL_CALL]:
            snapshot = ToolCallHookInput(
                run_id=run_id,
                turn_id=turn_id,
                call_id=value.call_id,
                tool_name=value.tool_name,
                arguments_json=json.dumps(
                    value.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            try:
                outcome = self._invoke_handler(registered, snapshot)
            except Exception as exc:
                self._emit(
                    ExtensionEventKind.HANDLER_FAILED,
                    extension_name=registered.extension_name,
                    hook=Hook.TOOL_CALL,
                    code="handler_exception",
                    message=f"{type(exc).__name__}: {exc}",
                )
                raise ExtensionDispatchError(
                    f"Extension {registered.extension_name!r} tool_call handler failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if isinstance(outcome, Observe):
                self._emit(
                    ExtensionEventKind.HANDLER_OUTCOME,
                    extension_name=registered.extension_name,
                    hook=Hook.TOOL_CALL,
                    outcome="observe",
                )
                continue
            if isinstance(outcome, Transform):
                candidate = outcome.value
                try:
                    if not isinstance(candidate, ToolCall):
                        raise ExtensionDispatchError("tool_call transform must produce a ToolCall")
                    if candidate.call_id != call.call_id:
                        raise ExtensionDispatchError(
                            "tool_call transform cannot change the authoritative call_id"
                        )
                    validator(candidate)
                except Exception as exc:
                    rejected = (
                        exc
                        if isinstance(exc, ExtensionDispatchError)
                        else ExtensionDispatchError(f"ToolCall revalidation failed: {exc}")
                    )
                    self._emit(
                        ExtensionEventKind.OUTCOME_REJECTED,
                        extension_name=registered.extension_name,
                        hook=Hook.TOOL_CALL,
                        outcome="transform",
                        code=rejected.code,
                        message=str(rejected),
                    )
                    raise rejected from exc
                value = copy.deepcopy(candidate)
                self._emit(
                    ExtensionEventKind.HANDLER_OUTCOME,
                    extension_name=registered.extension_name,
                    hook=Hook.TOOL_CALL,
                    outcome="transform",
                    revalidated=True,
                )
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.TOOL_CALL, outcome)
            self._emit(
                ExtensionEventKind.OUTCOME_REJECTED,
                extension_name=registered.extension_name,
                hook=Hook.TOOL_CALL,
                code="outcome_not_allowed",
                message="tool_call Hook permits only observe, transform, or block",
            )
            raise ExtensionDispatchError(
                "tool_call Hook permits only Observe, Transform, or Block outcomes"
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.TOOL_CALL)
        return value

    def transform_context(self, context: ModelContext) -> ModelContext:
        canonical_max_characters = context.max_characters
        canonical_assembly_order = context.assembly_order
        value = self._validate_context(
            context,
            max_characters=canonical_max_characters,
            assembly_order=canonical_assembly_order,
        )
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.CONTEXT)
        for registered in self._handlers[Hook.CONTEXT]:
            outcome_name: str | None = None
            try:
                outcome = self._invoke_handler(
                    registered,
                    ContextHookInput(copy.deepcopy(value)),
                )
            except Exception as exc:
                self._handler_failed(registered, Hook.CONTEXT, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.CONTEXT, "observe")
                continue
            try:
                if isinstance(outcome, Transform):
                    if not isinstance(outcome.value, ModelContext):
                        raise ExtensionDispatchError(
                            "context transform must produce a ModelContext"
                        )
                    candidate = outcome.value
                    outcome_name = "transform"
                elif isinstance(outcome, Supplement):
                    if not isinstance(outcome.value, ContextSupplement):
                        raise ExtensionDispatchError(
                            "context supplement must produce a ContextSupplement"
                        )
                    candidate = self._supplement_context(value, outcome.value)
                    outcome_name = "supplement"
                elif isinstance(outcome, Block):
                    self._block(registered, Hook.CONTEXT, outcome)
                else:
                    raise ExtensionDispatchError(
                        "context Hook permits Observe, Transform, Supplement, or Block"
                    )
                value = self._validate_context(
                    candidate,
                    max_characters=canonical_max_characters,
                    assembly_order=canonical_assembly_order,
                )
            except ExtensionBlockedError:
                raise
            except Exception as exc:
                rejected = (
                    exc
                    if isinstance(exc, ExtensionDispatchError)
                    else ExtensionDispatchError(f"Context revalidation failed: {exc}")
                )
                self._emit(
                    ExtensionEventKind.OUTCOME_REJECTED,
                    extension_name=registered.extension_name,
                    hook=Hook.CONTEXT,
                    outcome=outcome_name,
                    code=rejected.code,
                    message=str(rejected),
                )
                raise rejected from exc
            assert outcome_name is not None
            self._handler_outcome(
                registered,
                Hook.CONTEXT,
                outcome_name,
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.CONTEXT)
        return value

    def transform_provider_request(
        self,
        request: ProviderRequest,
        *,
        max_characters: int,
    ) -> ProviderRequest:
        value = self._validate_provider_request(request, max_characters=max_characters)
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.PROVIDER_REQUEST)
        for registered in self._handlers[Hook.PROVIDER_REQUEST]:
            try:
                outcome = self._invoke_handler(
                    registered,
                    ProviderRequestHookInput(copy.deepcopy(value)),
                )
            except Exception as exc:
                self._handler_failed(registered, Hook.PROVIDER_REQUEST, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.PROVIDER_REQUEST, "observe")
                continue
            try:
                if isinstance(outcome, Transform):
                    candidate = outcome.value
                    outcome_name = "transform"
                elif isinstance(outcome, Supplement):
                    supplement = outcome.value
                    if not isinstance(supplement, ProviderRequestSupplement):
                        raise ExtensionDispatchError(
                            "provider_request supplement must produce ProviderRequestSupplement"
                        )
                    candidate = ProviderRequest(
                        messages=(*value.messages, *supplement.messages),
                        tools=value.tools,
                        system_prompt=value.system_prompt,
                        tool_guidelines=value.tool_guidelines,
                        project_context=(*value.project_context, *supplement.project_context),
                    )
                    outcome_name = "supplement"
                elif isinstance(outcome, Block):
                    self._block(registered, Hook.PROVIDER_REQUEST, outcome)
                else:
                    raise ExtensionDispatchError(
                        "provider_request Hook permits Observe, Transform, Supplement, or Block"
                    )
                value = self._validate_provider_request(
                    candidate,
                    max_characters=max_characters,
                )
            except ExtensionBlockedError:
                raise
            except Exception as exc:
                self._reject_outcome(registered, Hook.PROVIDER_REQUEST, exc)
            self._handler_outcome(
                registered,
                Hook.PROVIDER_REQUEST,
                outcome_name,
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.PROVIDER_REQUEST)
        return value

    def transform_provider_response(
        self,
        request: ProviderRequest,
        event: ProviderStreamEvent,
        *,
        attempt: int,
    ) -> tuple[ProviderStreamEvent, tuple[str, ...]]:
        value = self._validate_provider_event(event)
        transformed_by: list[str] = []
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.PROVIDER_RESPONSE)
        for registered in self._handlers[Hook.PROVIDER_RESPONSE]:
            try:
                outcome = self._invoke_handler(
                    registered,
                    ProviderResponseHookInput(
                        copy.deepcopy(request),
                        copy.deepcopy(value),
                        attempt,
                    ),
                )
            except Exception as exc:
                self._handler_failed(registered, Hook.PROVIDER_RESPONSE, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.PROVIDER_RESPONSE, "observe")
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.PROVIDER_RESPONSE, outcome)
            if not isinstance(outcome, Transform):
                self._reject_outcome(
                    registered,
                    Hook.PROVIDER_RESPONSE,
                    ExtensionDispatchError(
                        "provider_response Hook permits only Observe, Transform, or Block"
                    ),
                )
            try:
                candidate = self._validate_provider_event(outcome.value)
                if type(candidate) is not type(value):
                    raise ExtensionDispatchError(
                        "provider_response transforms must preserve the event kind"
                    )
                if isinstance(
                    value, (ProviderToolCallStart, ProviderToolCallDelta, ProviderToolCallEnd)
                ):
                    if (
                        not isinstance(
                            candidate,
                            (ProviderToolCallStart, ProviderToolCallDelta, ProviderToolCallEnd),
                        )
                        or candidate.index != value.index
                    ):
                        raise ExtensionDispatchError(
                            "provider_response transforms cannot change a ToolCall index"
                        )
                value = copy.deepcopy(candidate)
            except Exception as exc:
                self._reject_outcome(registered, Hook.PROVIDER_RESPONSE, exc)
            transformed_by.append(registered.extension_name)
            self._handler_outcome(
                registered,
                Hook.PROVIDER_RESPONSE,
                "transform",
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.PROVIDER_RESPONSE)
        return value, tuple(transformed_by)

    def transform_message_end(
        self,
        message: AssistantMessage,
        *,
        run_id: str,
        turn_id: str,
        message_id: str,
    ) -> AssistantMessage:
        value = self._validate_assistant_message(message)
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.MESSAGE_END)
        for registered in self._handlers[Hook.MESSAGE_END]:
            try:
                outcome = self._invoke_handler(
                    registered,
                    MessageHookInput(
                        run_id,
                        turn_id,
                        message_id,
                        copy.deepcopy(value),
                    ),
                )
            except Exception as exc:
                self._handler_failed(registered, Hook.MESSAGE_END, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.MESSAGE_END, "observe")
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.MESSAGE_END, outcome)
            if not isinstance(outcome, Transform):
                self._reject_outcome(
                    registered,
                    Hook.MESSAGE_END,
                    ExtensionDispatchError(
                        "message_end Hook permits only Observe, Transform, or Block"
                    ),
                )
            try:
                value = self._validate_assistant_message(outcome.value)
            except Exception as exc:
                self._reject_outcome(registered, Hook.MESSAGE_END, exc)
            self._handler_outcome(
                registered,
                Hook.MESSAGE_END,
                "transform",
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.MESSAGE_END)
        return value

    def transform_tool_result(
        self,
        result: ToolResult,
        *,
        run_id: str,
        turn_id: str,
    ) -> ToolResult:
        original = self._validate_tool_result(result)
        value = original
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.TOOL_RESULT)
        for registered in self._handlers[Hook.TOOL_RESULT]:
            snapshot = ToolResultHookInput(
                run_id=run_id,
                turn_id=turn_id,
                call_id=value.call_id,
                tool_name=value.tool_name,
                status=value.status,
                output_json=(
                    None
                    if value.output is None
                    else json.dumps(
                        value.output,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                error_code=None if value.error is None else value.error.code,
                error_message=None if value.error is None else value.error.message,
            )
            try:
                outcome = self._invoke_handler(registered, snapshot)
            except Exception as exc:
                self._handler_failed(registered, Hook.TOOL_RESULT, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.TOOL_RESULT, "observe")
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.TOOL_RESULT, outcome)
            try:
                if isinstance(outcome, Transform):
                    candidate = outcome.value
                    outcome_name = "transform"
                elif isinstance(outcome, Supplement):
                    supplement = outcome.value
                    if not isinstance(supplement, ToolResultSupplement):
                        raise ExtensionDispatchError(
                            "tool_result supplement must produce ToolResultSupplement"
                        )
                    output = dict(value.output or {})
                    output.update(dict(supplement.output))
                    candidate = ToolResult(
                        value.call_id,
                        value.tool_name,
                        value.status,
                        output,
                        value.error,
                    )
                    outcome_name = "supplement"
                else:
                    raise ExtensionDispatchError(
                        "tool_result Hook permits Observe, Transform, Supplement, or Block"
                    )
                validated = self._validate_tool_result(candidate)
                if (
                    validated.call_id != original.call_id
                    or validated.tool_name != original.tool_name
                ):
                    raise ExtensionDispatchError(
                        "tool_result handlers cannot change call_id or tool_name"
                    )
                if original.status != "success" and validated.status == "success":
                    raise ExtensionDispatchError(
                        "tool_result handlers cannot turn an unexecuted or failed result "
                        "into success"
                    )
                value = validated
            except Exception as exc:
                self._reject_outcome(registered, Hook.TOOL_RESULT, exc)
            self._handler_outcome(
                registered,
                Hook.TOOL_RESULT,
                outcome_name,
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.TOOL_RESULT)
        return value

    def before_agent_start(self, hook_input: BeforeAgentStartHookInput) -> None:
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.BEFORE_AGENT_START)
        for registered in self._handlers[Hook.BEFORE_AGENT_START]:
            try:
                outcome = self._invoke_handler(registered, hook_input)
            except Exception as exc:
                self._handler_failed(registered, Hook.BEFORE_AGENT_START, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.BEFORE_AGENT_START, "observe")
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.BEFORE_AGENT_START, outcome)
            self._reject_outcome(
                registered,
                Hook.BEFORE_AGENT_START,
                ExtensionDispatchError("before_agent_start Hook permits only Observe or Block"),
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.BEFORE_AGENT_START)

    def transform_compaction(
        self,
        plan: CompactionPlan,
        *,
        run_id: str,
        session_id: str,
        active_branch: tuple[str, ...],
        validator: Callable[[CompactionPlan], None],
    ) -> CompactionPlan:
        validator(plan)
        value = plan
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=Hook.COMPACTION_START)
        for registered in self._handlers[Hook.COMPACTION_START]:
            hook_input = CompactionHookInput(
                run_id,
                session_id,
                active_branch,
                plan=value,
            )
            try:
                outcome = self._invoke_handler(registered, hook_input)
            except Exception as exc:
                self._handler_failed(registered, Hook.COMPACTION_START, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, Hook.COMPACTION_START, "observe")
                continue
            if isinstance(outcome, Block):
                self._block(registered, Hook.COMPACTION_START, outcome)
            if not isinstance(outcome, Transform) or not isinstance(outcome.value, CompactionPlan):
                self._reject_outcome(
                    registered,
                    Hook.COMPACTION_START,
                    ExtensionDispatchError(
                        "compaction_start Hook permits Observe, Transform, or Block"
                    ),
                )
            try:
                validator(outcome.value)
            except Exception as exc:
                self._reject_outcome(registered, Hook.COMPACTION_START, exc)
            value = outcome.value
            self._handler_outcome(
                registered,
                Hook.COMPACTION_START,
                "transform",
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=Hook.COMPACTION_START)
        return value

    def observe_runtime_event(self, event: AgentEvent | AgentSessionEvent) -> None:
        if isinstance(event, AgentEvent):
            mapping = {
                AgentEventKind.AGENT_START: Hook.AGENT_START,
                AgentEventKind.AGENT_END: Hook.AGENT_END,
                AgentEventKind.TURN_START: Hook.TURN_START,
                AgentEventKind.TURN_END: Hook.TURN_END,
                AgentEventKind.MESSAGE_START: Hook.MESSAGE_START,
                AgentEventKind.MESSAGE_UPDATE: Hook.MESSAGE_UPDATE,
                AgentEventKind.TOOL_EXECUTION_START: Hook.TOOL_EXECUTION_START,
                AgentEventKind.TOOL_EXECUTION_UPDATE: Hook.TOOL_EXECUTION_UPDATE,
                AgentEventKind.TOOL_EXECUTION_END: Hook.TOOL_EXECUTION_END,
            }
            hook = mapping.get(event.kind)
            if hook is None:
                return
            if hook in {Hook.MESSAGE_START, Hook.MESSAGE_UPDATE}:
                if event.turn_id is None or event.message_id is None or event.message is None:
                    raise ExtensionDispatchError(f"{hook.value} event snapshot is incomplete")
                hook_input: object = MessageHookInput(
                    event.run_id,
                    event.turn_id,
                    event.message_id,
                    copy.deepcopy(event.message),
                    copy.deepcopy(event.provider_event),
                )
            elif hook in {
                Hook.TOOL_EXECUTION_START,
                Hook.TOOL_EXECUTION_UPDATE,
                Hook.TOOL_EXECUTION_END,
            }:
                if event.turn_id is None:
                    raise ExtensionDispatchError(f"{hook.value} event has no turn_id")
                hook_input = ToolExecutionHookInput(
                    event.run_id,
                    event.turn_id,
                    tool_call=copy.deepcopy(event.tool_call),
                    tool_result=copy.deepcopy(event.tool_result),
                    progress_stream=(
                        None if event.tool_progress is None else event.tool_progress.stream
                    ),
                    progress_data=(
                        None if event.tool_progress is None else event.tool_progress.data
                    ),
                    batch_mode=event.batch_mode,
                )
            else:
                hook_input = LifecycleHookInput(event.run_id, event.turn_id)
            self._observe_only(hook, hook_input)
            return

        session_mapping = {
            AgentSessionEventKind.SESSION_CONFIGURATION: Hook.SESSION_CONFIGURATION,
            AgentSessionEventKind.SESSION_ENTRY: Hook.SESSION_ENTRY,
            AgentSessionEventKind.SESSION_RESUMED: Hook.SESSION_RESUMED,
            AgentSessionEventKind.ACTIVE_BRANCH: Hook.SESSION_TREE,
            AgentSessionEventKind.COMPACTION_SUCCEEDED: Hook.COMPACTION_END,
            AgentSessionEventKind.COMPACTION_FAILED: Hook.COMPACTION_FAILED,
        }
        hook = session_mapping.get(event.kind)
        if hook is None or event.session_id is None:
            return
        if hook in {Hook.COMPACTION_END, Hook.COMPACTION_FAILED}:
            hook_input = CompactionHookInput(
                event.run_id,
                event.session_id,
                event.active_branch or (),
                entry=event.session_entry,
                error=event.context_error,
            )
        else:
            hook_input = SessionHookInput(
                event.run_id,
                event.session_id,
                entry=event.session_entry,
                active_branch=event.active_branch,
                configuration_json=event.configuration_json,
            )
        self._observe_only(hook, hook_input)

    def settle_agent(
        self,
        result: AgentRunResult,
        *,
        validator: Callable[[str, Mapping[str, object]], None],
    ) -> tuple[SessionEntryDraft, ...]:
        drafts: list[SessionEntryDraft] = []
        hook = Hook.AGENT_SETTLED
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=hook)
        for registered in self._handlers[hook]:
            try:
                outcome = self._invoke_handler(
                    registered,
                    AgentSettledHookInput(copy.deepcopy(result)),
                )
            except Exception as exc:
                self._handler_failed(registered, hook, exc)
            if isinstance(outcome, Observe):
                self._handler_outcome(registered, hook, "observe")
                continue
            if not isinstance(outcome, Supplement) or not isinstance(
                outcome.value, SessionEntryDraft
            ):
                self._reject_outcome(
                    registered,
                    hook,
                    ExtensionDispatchError(
                        "agent_settled Hook permits Observe or a SessionEntryDraft Supplement"
                    ),
                )
            try:
                if not isinstance(outcome.value.kind, str) or not outcome.value.kind:
                    raise ExtensionDispatchError(
                        "SessionEntryDraft kind must be a non-empty string"
                    )
                if not isinstance(outcome.value.payload, Mapping):
                    raise ExtensionDispatchError("SessionEntryDraft payload must be a mapping")
                draft = SessionEntryDraft(
                    outcome.value.kind,
                    copy.deepcopy(dict(outcome.value.payload)),
                )
                validator(draft.kind, draft.payload)
            except Exception as exc:
                self._reject_outcome(registered, hook, exc)
            drafts.append(draft)
            self._handler_outcome(
                registered,
                hook,
                "supplement",
                revalidated=True,
            )
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=hook)
        return tuple(drafts)

    def _observe_only(self, hook: Hook, hook_input: object) -> None:
        self._emit(ExtensionEventKind.DISPATCH_STARTED, hook=hook)
        for registered in self._handlers[hook]:
            try:
                outcome = self._invoke_handler(registered, copy.deepcopy(hook_input))
            except Exception as exc:
                self._handler_failed(registered, hook, exc)
            if not isinstance(outcome, Observe):
                self._reject_outcome(
                    registered,
                    hook,
                    ExtensionDispatchError(f"{hook.value} Hook permits only Observe"),
                )
            self._handler_outcome(registered, hook, "observe")
        self._emit(ExtensionEventKind.DISPATCH_COMPLETED, hook=hook)

    def drain_events(self) -> tuple[ExtensionEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def record_settlement_failure(self, error: Exception) -> None:
        """Publish a rejected post-terminal supplement without changing Run state."""

        self._emit(
            ExtensionEventKind.OUTCOME_REJECTED,
            hook=Hook.AGENT_SETTLED,
            outcome="supplement",
            code="settlement_persistence_failed",
            message=f"{type(error).__name__}: {error}",
        )

    def record_provider_composition_failure(
        self,
        extension_names: tuple[str, ...],
        error: Exception,
    ) -> None:
        """Attribute delayed stream-contract failure to transforming handlers."""

        message = f"provider_response transform violated the stream contract: {error}"
        for extension_name in dict.fromkeys(extension_names):
            self._emit(
                ExtensionEventKind.OUTCOME_REJECTED,
                extension_name=extension_name,
                hook=Hook.PROVIDER_RESPONSE,
                outcome="transform",
                code="stream_contract_violation",
                message=message,
            )

    def record_provider_cleanup_failure(self, error: BaseException) -> None:
        """Expose a secondary Provider cleanup failure without changing Run state."""

        self._emit(
            ExtensionEventKind.RUNTIME_FAILURE,
            hook=Hook.PROVIDER_RESPONSE,
            code="provider_cleanup_failed",
            message=f"{type(error).__name__}: {error}",
        )

    @staticmethod
    def _supplement_context(
        context: ModelContext,
        supplement: ContextSupplement,
    ) -> ModelContext:
        request = context.provider_request
        supplemented = ProviderRequest(
            messages=(*request.messages, *supplement.messages),
            tools=request.tools,
            system_prompt=request.system_prompt,
            tool_guidelines=request.tool_guidelines,
            project_context=(*request.project_context, *supplement.project_context),
        )
        return ModelContext(
            provider_request=supplemented,
            estimated_characters=0,
            max_characters=context.max_characters,
            assembly_order=context.assembly_order,
        )

    @staticmethod
    def _validate_context(
        value: object,
        *,
        max_characters: int,
        assembly_order: tuple[str, ...],
    ) -> ModelContext:
        if not isinstance(value, ModelContext):
            raise ExtensionDispatchError("context result must be a ModelContext")
        if value.max_characters != max_characters:
            raise ExtensionDispatchError(
                "context handlers cannot change the Kernel-owned character budget"
            )
        if value.assembly_order != assembly_order:
            raise ExtensionDispatchError(
                "context handlers cannot change the canonical assembly order"
            )
        request = value.provider_request
        request = ExtensionRuntime._validate_provider_request(
            request,
            max_characters=max_characters,
        )
        estimated = estimate_provider_request_characters(request)
        return ModelContext(
            provider_request=request,
            estimated_characters=estimated,
            max_characters=max_characters,
            assembly_order=assembly_order,
        )

    @staticmethod
    def _validate_provider_request(
        value: object,
        *,
        max_characters: int,
    ) -> ProviderRequest:
        if not isinstance(value, ProviderRequest):
            raise ExtensionDispatchError(
                "provider_request transform must produce a ProviderRequest"
            )
        if not isinstance(value.messages, tuple) or not isinstance(value.tools, tuple):
            raise ExtensionDispatchError("ProviderRequest collections must be tuples")
        messages = tuple(
            ExtensionRuntime._validate_model_message(message) for message in value.messages
        )
        if not all(isinstance(tool, dict) for tool in value.tools):
            raise ExtensionDispatchError("ProviderRequest Tool schemas must be dictionaries")
        try:
            tools = tuple(
                json_object_snapshot(tool, label="ProviderRequest Tool schema")
                for tool in value.tools
            )
        except ValueError as exc:
            raise ExtensionDispatchError(str(exc)) from exc
        if not isinstance(value.system_prompt, str) or not isinstance(value.tool_guidelines, str):
            raise ExtensionDispatchError("ProviderRequest prompt fields must be strings")
        if not isinstance(value.project_context, tuple):
            raise ExtensionDispatchError("ProviderRequest project resources must be a tuple")
        if not all(isinstance(item, str) for item in value.project_context):
            raise ExtensionDispatchError("ProviderRequest project resources must be strings")
        snapshot = ProviderRequest(
            messages=messages,
            tools=tools,
            system_prompt=value.system_prompt,
            tool_guidelines=value.tool_guidelines,
            project_context=value.project_context,
        )
        try:
            estimated = estimate_provider_request_characters(snapshot)
        except Exception as exc:
            raise ExtensionDispatchError(
                f"ProviderRequest revalidation failed: {type(exc).__name__}: {exc}"
            ) from exc
        if estimated > max_characters:
            raise ExtensionDispatchError(
                "Extension ProviderRequest exceeds the canonical character budget"
            )
        return snapshot

    @staticmethod
    def _validate_model_message(value: object) -> ModelMessage:
        if isinstance(value, UserMessage):
            if value.role != "user" or not isinstance(value.text, str):
                raise ExtensionDispatchError("UserMessage must preserve its role and text type")
            return UserMessage(text=value.text)
        if isinstance(value, BranchSummaryMessage):
            if value.role != "summary" or not isinstance(value.text, str):
                raise ExtensionDispatchError(
                    "BranchSummaryMessage must preserve its role and text type"
                )
            return BranchSummaryMessage(text=value.text)
        if isinstance(value, AssistantMessage):
            return ExtensionRuntime._validate_assistant_message(value)
        if isinstance(value, ToolResultMessage):
            if value.role != "tool" or not isinstance(value.results, tuple):
                raise ExtensionDispatchError("ToolResultMessage contract is invalid")
            return ToolResultMessage(
                results=tuple(
                    ExtensionRuntime._validate_tool_result(result) for result in value.results
                )
            )
        raise ExtensionDispatchError("ProviderRequest contains an unknown ModelMessage type")

    @staticmethod
    def _validate_provider_event(value: object) -> ProviderStreamEvent:
        try:
            return validate_provider_stream_event(value)
        except ValueError as exc:
            raise ExtensionDispatchError(str(exc)) from exc

    @staticmethod
    def _validate_assistant_message(value: object) -> AssistantMessage:
        if not isinstance(value, AssistantMessage):
            raise ExtensionDispatchError("message_end transform must produce an AssistantMessage")
        if value.role != "assistant":
            raise ExtensionDispatchError("AssistantMessage role must remain 'assistant'")
        if not isinstance(value.text, str) or not isinstance(value.thinking, str):
            raise ExtensionDispatchError("AssistantMessage text fields must be strings")
        if not isinstance(value.tool_calls, tuple):
            raise ExtensionDispatchError("AssistantMessage tool_calls must be a tuple")
        call_ids: set[str] = set()
        safe_calls: list[ToolCall] = []
        for call in value.tool_calls:
            if (
                not isinstance(call, ToolCall)
                or not isinstance(call.call_id, str)
                or not call.call_id
                or not isinstance(call.tool_name, str)
                or not call.tool_name
                or not isinstance(call.arguments, dict)
            ):
                raise ExtensionDispatchError("AssistantMessage contains an invalid ToolCall")
            if call.call_id in call_ids:
                raise ExtensionDispatchError("AssistantMessage ToolCall IDs must be unique")
            call_ids.add(call.call_id)
            try:
                arguments = json_object_snapshot(
                    call.arguments,
                    label="AssistantMessage ToolCall arguments",
                )
            except ValueError as exc:
                raise ExtensionDispatchError(str(exc)) from exc
            safe_calls.append(ToolCall(call.call_id, call.tool_name, arguments))
        if value.usage is not None:
            if not isinstance(value.usage, TokenUsage) or any(
                type(tokens) is not int or tokens < 0
                for tokens in (value.usage.input_tokens, value.usage.output_tokens)
            ):
                raise ExtensionDispatchError("AssistantMessage usage is invalid")
        if value.stop_reason is not None and not isinstance(value.stop_reason, str):
            raise ExtensionDispatchError("AssistantMessage stop_reason must be a string or None")
        if value.response_id is not None and not isinstance(value.response_id, str):
            raise ExtensionDispatchError("AssistantMessage response_id must be a string or None")
        try:
            snapshot = AssistantMessage(
                role="assistant",
                text=value.text,
                thinking=value.thinking,
                tool_calls=tuple(safe_calls),
                usage=copy.deepcopy(value.usage),
                stop_reason=value.stop_reason,
                response_id=value.response_id,
            )
            json_object_snapshot(
                assistant_message_record(snapshot),
                label="AssistantMessage",
            )
        except ValueError as exc:
            raise ExtensionDispatchError(
                f"AssistantMessage revalidation failed: {type(exc).__name__}: {exc}"
            ) from exc
        return snapshot

    @staticmethod
    def _validate_tool_result(value: object) -> ToolResult:
        if not isinstance(value, ToolResult):
            raise ExtensionDispatchError("tool_result transform must produce a ToolResult")
        if not isinstance(value.call_id, str) or not value.call_id:
            raise ExtensionDispatchError("ToolResult call_id must be a non-empty string")
        if not isinstance(value.tool_name, str) or not value.tool_name:
            raise ExtensionDispatchError("ToolResult tool_name must be a non-empty string")
        if value.status not in {"success", "error", "cancelled"}:
            raise ExtensionDispatchError("ToolResult status is invalid")
        if value.output is not None and not isinstance(value.output, dict):
            raise ExtensionDispatchError("ToolResult output must be a dictionary or None")
        if value.status == "success" and value.error is not None:
            raise ExtensionDispatchError("successful ToolResult cannot contain an error")
        if value.status != "success" and value.error is None:
            raise ExtensionDispatchError("non-success ToolResult requires an error")
        if value.error is not None and (
            not isinstance(value.error, ToolError)
            or not isinstance(value.error.code, str)
            or not value.error.code
            or not isinstance(value.error.message, str)
        ):
            raise ExtensionDispatchError("ToolResult error contract is invalid")
        try:
            output = (
                None
                if value.output is None
                else json_object_snapshot(value.output, label="ToolResult output")
            )
        except ValueError as exc:
            raise ExtensionDispatchError(
                f"ToolResult output is not JSON serializable: {type(exc).__name__}: {exc}"
            ) from exc
        return ToolResult(
            value.call_id,
            value.tool_name,
            value.status,
            output,
            copy.deepcopy(value.error),
        )

    def _reject_outcome(
        self,
        registered: _RegisteredHandler,
        hook: Hook,
        error: Exception,
    ) -> NoReturn:
        rejected = (
            error
            if isinstance(error, ExtensionDispatchError)
            else ExtensionDispatchError(f"{hook.value} revalidation failed: {error}")
        )
        self._emit(
            ExtensionEventKind.OUTCOME_REJECTED,
            extension_name=registered.extension_name,
            hook=hook,
            code=rejected.code,
            message=str(rejected),
        )
        raise rejected from error

    def _block(
        self,
        registered: _RegisteredHandler,
        hook: Hook,
        outcome: Block,
    ) -> NoReturn:
        if (
            not isinstance(outcome.code, str)
            or not outcome.code
            or not isinstance(outcome.message, str)
            or not outcome.message
        ):
            self._reject_outcome(
                registered,
                hook,
                ExtensionDispatchError(
                    f"{hook.value} Block requires non-empty string code and message"
                ),
            )
        self._emit(
            ExtensionEventKind.DISPATCH_BLOCKED,
            extension_name=registered.extension_name,
            hook=hook,
            outcome="block",
            code=outcome.code,
            message=outcome.message,
        )
        raise ExtensionBlockedError(outcome.code, outcome.message)

    def _handler_failed(
        self,
        registered: _RegisteredHandler,
        hook: Hook,
        error: Exception,
    ) -> NoReturn:
        self._emit(
            ExtensionEventKind.HANDLER_FAILED,
            extension_name=registered.extension_name,
            hook=hook,
            code="handler_exception",
            message=f"{type(error).__name__}: {error}",
        )
        raise ExtensionDispatchError(
            f"Extension {registered.extension_name!r} {hook.value} handler failed: "
            f"{type(error).__name__}: {error}"
        ) from error

    @staticmethod
    def _invoke_handler(registered: _RegisteredHandler, hook_input: object) -> HookOutcome:
        try:
            outcome = invoke_sync_callout(registered.handler, hook_input)
        except asyncio.CancelledError as exc:
            raise ExtensionDispatchError(
                "Extension handlers cannot cancel the authoritative Agent Run"
            ) from exc
        if inspect.isawaitable(outcome):
            dispose_awaitable(outcome)
            raise ExtensionDispatchError(
                "Extension Hook handlers must return an outcome synchronously"
            )
        return outcome

    def _handler_outcome(
        self,
        registered: _RegisteredHandler,
        hook: Hook,
        outcome: str,
        *,
        revalidated: bool = False,
    ) -> None:
        self._emit(
            ExtensionEventKind.HANDLER_OUTCOME,
            extension_name=registered.extension_name,
            hook=hook,
            outcome=outcome,
            revalidated=revalidated,
        )

    @staticmethod
    def _validate_prompt(value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ExtensionDispatchError("input transform must produce a non-empty string")

    def _emit(
        self,
        kind: ExtensionEventKind,
        *,
        extension_name: str | None = None,
        hook: Hook | None = None,
        capability: str | None = None,
        outcome: str | None = None,
        revalidated: bool = False,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        self._events.append(
            ExtensionEvent(
                sequence=next(self._sequence),
                kind=kind,
                extension_name=extension_name,
                hook=hook,
                capability=capability,
                outcome=outcome,
                revalidated=revalidated,
                code=code,
                message=message,
            )
        )
