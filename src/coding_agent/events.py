"""Typed event and result values crossing the Kernel's public seams."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, TypeAlias


class ProviderEventKind(StrEnum):
    """Normalized kinds emitted by a streaming model provider."""

    STREAM_START = "stream_start"
    STREAM_END = "stream_end"
    CONTENT_START = "content_start"
    CONTENT_END = "content_end"
    TEXT_START = "text_start"
    TEXT_DELTA = "text_delta"
    TEXT_END = "text_end"
    THINKING_START = "thinking_start"
    THINKING_DELTA = "thinking_delta"
    THINKING_END = "thinking_end"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"
    ABORT = "abort"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProviderStreamStart:
    model: str | None = None
    kind: Literal[ProviderEventKind.STREAM_START] = field(
        default=ProviderEventKind.STREAM_START, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderStreamEnd:
    kind: Literal[ProviderEventKind.STREAM_END] = field(
        default=ProviderEventKind.STREAM_END, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderContentStart:
    kind: Literal[ProviderEventKind.CONTENT_START] = field(
        default=ProviderEventKind.CONTENT_START, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderContentEnd:
    kind: Literal[ProviderEventKind.CONTENT_END] = field(
        default=ProviderEventKind.CONTENT_END, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderTextStart:
    kind: Literal[ProviderEventKind.TEXT_START] = field(
        default=ProviderEventKind.TEXT_START, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    """A text fragment from the provider."""

    delta: str
    kind: Literal[ProviderEventKind.TEXT_DELTA] = field(
        default=ProviderEventKind.TEXT_DELTA,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class ProviderTextEnd:
    kind: Literal[ProviderEventKind.TEXT_END] = field(
        default=ProviderEventKind.TEXT_END, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderThinkingStart:
    kind: Literal[ProviderEventKind.THINKING_START] = field(
        default=ProviderEventKind.THINKING_START, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderThinkingDelta:
    """A thinking fragment from the provider."""

    delta: str
    kind: Literal[ProviderEventKind.THINKING_DELTA] = field(
        default=ProviderEventKind.THINKING_DELTA,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class ProviderThinkingEnd:
    kind: Literal[ProviderEventKind.THINKING_END] = field(
        default=ProviderEventKind.THINKING_END, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderToolCallStart:
    index: int
    call_id: str = ""
    tool_name: str = ""
    kind: Literal[ProviderEventKind.TOOL_CALL_START] = field(
        default=ProviderEventKind.TOOL_CALL_START, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderToolCallDelta:
    index: int
    call_id_delta: str = ""
    tool_name_delta: str = ""
    arguments_delta: str = ""
    kind: Literal[ProviderEventKind.TOOL_CALL_DELTA] = field(
        default=ProviderEventKind.TOOL_CALL_DELTA, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderToolCallEnd:
    index: int
    kind: Literal[ProviderEventKind.TOOL_CALL_END] = field(
        default=ProviderEventKind.TOOL_CALL_END, init=False
    )


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    kind: Literal[ProviderEventKind.USAGE] = field(default=ProviderEventKind.USAGE, init=False)


@dataclass(frozen=True, slots=True)
class ProviderDone:
    """The provider's authoritative successful stream terminator."""

    stop_reason: str = "stop"
    response_id: str | None = None
    kind: Literal[ProviderEventKind.DONE] = field(
        default=ProviderEventKind.DONE,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class ProviderError:
    """A normalized provider failure carried inside the stream."""

    code: str
    message: str
    kind: Literal[ProviderEventKind.ERROR] = field(
        default=ProviderEventKind.ERROR,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class ProviderAbort:
    reason: str
    kind: Literal[ProviderEventKind.ABORT] = field(default=ProviderEventKind.ABORT, init=False)


@dataclass(frozen=True, slots=True)
class ProviderCancelled:
    reason: str
    kind: Literal[ProviderEventKind.CANCELLED] = field(
        default=ProviderEventKind.CANCELLED, init=False
    )


ProviderStreamEvent: TypeAlias = (
    ProviderStreamStart
    | ProviderStreamEnd
    | ProviderContentStart
    | ProviderContentEnd
    | ProviderTextStart
    | ProviderTextDelta
    | ProviderTextEnd
    | ProviderThinkingStart
    | ProviderThinkingDelta
    | ProviderThinkingEnd
    | ProviderToolCallStart
    | ProviderToolCallDelta
    | ProviderToolCallEnd
    | ProviderUsage
    | ProviderDone
    | ProviderError
    | ProviderAbort
    | ProviderCancelled
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A complete model-authored tool request."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: Literal["success", "error", "cancelled"]
    output: dict[str, Any] | None = None
    error: ToolError | None = None


@dataclass(frozen=True, slots=True)
class ToolProgress:
    call_id: str
    tool_name: str
    stream: str
    data: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """The cumulative assistant message assembled from provider increments."""

    role: Literal["assistant"] = "assistant"
    text: str = ""
    thinking: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    response_id: str | None = None

    def apply(self, event: ProviderStreamEvent) -> AssistantMessage:
        """Return the cumulative message after applying one provider event."""

        accumulator = AssistantMessageAccumulator(self)
        return accumulator.apply(event)


@dataclass(slots=True)
class _PartialToolCall:
    call_id: str = ""
    tool_name: str = ""
    arguments: str = ""


class AssistantMessageAccumulator:
    """Accumulate one provider response without losing current stream events."""

    def __init__(self, message: AssistantMessage | None = None) -> None:
        self._message = message or AssistantMessage()
        self._partial_tool_calls: dict[int, _PartialToolCall] = {}
        self._completed_tool_calls: dict[int, ToolCall] = dict(enumerate(self._message.tool_calls))

    @property
    def message(self) -> AssistantMessage:
        return self._message

    def apply(self, event: ProviderStreamEvent) -> AssistantMessage:
        message = self._message
        if isinstance(event, ProviderTextDelta):
            message = self._replace(text=message.text + event.delta)
        elif isinstance(event, ProviderThinkingDelta):
            message = self._replace(thinking=message.thinking + event.delta)
        elif isinstance(event, ProviderToolCallStart):
            self._partial_tool_calls[event.index] = _PartialToolCall(
                call_id=event.call_id, tool_name=event.tool_name
            )
        elif isinstance(event, ProviderToolCallDelta):
            partial = self._partial_tool_calls.setdefault(event.index, _PartialToolCall())
            partial.call_id += event.call_id_delta
            partial.tool_name += event.tool_name_delta
            partial.arguments += event.arguments_delta
        elif isinstance(event, ProviderToolCallEnd):
            partial = self._partial_tool_calls.pop(event.index)
            parsed = json.loads(partial.arguments or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("ToolCall arguments must decode to an object")
            self._completed_tool_calls[event.index] = ToolCall(
                partial.call_id, partial.tool_name, parsed
            )
            message = self._replace(
                tool_calls=tuple(
                    self._completed_tool_calls[index]
                    for index in sorted(self._completed_tool_calls)
                )
            )
        elif isinstance(event, ProviderUsage):
            message = self._replace(usage=TokenUsage(event.input_tokens, event.output_tokens))
        elif isinstance(event, ProviderDone):
            message = self._replace(
                stop_reason=event.stop_reason,
                response_id=event.response_id,
            )
        self._message = message
        return message

    def _replace(self, **changes: Any) -> AssistantMessage:
        values: dict[str, Any] = {
            "text": self._message.text,
            "thinking": self._message.thinking,
            "tool_calls": self._message.tool_calls,
            "usage": self._message.usage,
            "stop_reason": self._message.stop_reason,
            "response_id": self._message.response_id,
        }
        values.update(changes)
        return AssistantMessage(**values)


@dataclass(frozen=True, slots=True)
class AgentError:
    """A stable Kernel error independent of a provider implementation."""

    code: str
    message: str
    source: Literal["provider", "kernel"]


class AgentEventKind(StrEnum):
    """Low-level AgentLoop lifecycle event kinds."""

    AGENT_START = "agent_start"
    TURN_START = "turn_start"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_UPDATE = "tool_execution_update"
    TOOL_EXECUTION_END = "tool_execution_end"
    ERROR = "error"
    TURN_END = "turn_end"
    AGENT_END = "agent_end"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One low-level AgentLoop lifecycle event."""

    kind: AgentEventKind
    run_id: str
    turn_id: str | None = None
    message_id: str | None = None
    message: AssistantMessage | None = None
    provider_event: ProviderStreamEvent | None = None
    error: AgentError | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    tool_progress: ToolProgress | None = None
    batch_mode: Literal["parallel", "sequential"] | None = None

    def __post_init__(self) -> None:
        if self.kind is AgentEventKind.MESSAGE_UPDATE:
            if self.message is None or self.provider_event is None:
                raise ValueError("message_update requires a message and provider_event")
        elif self.kind is AgentEventKind.MESSAGE_END and self.message is None:
            raise ValueError("message_end requires the authoritative message")
        elif self.kind is AgentEventKind.ERROR and self.error is None:
            raise ValueError("error requires a structured AgentError")
        elif self.kind is AgentEventKind.TOOL_EXECUTION_START and self.tool_call is None:
            raise ValueError("tool_execution_start requires a ToolCall")
        elif self.kind is AgentEventKind.TOOL_EXECUTION_END and self.tool_result is None:
            raise ValueError("tool_execution_end requires a ToolResult")
        elif self.kind is AgentEventKind.TOOL_EXECUTION_UPDATE and self.tool_progress is None:
            raise ValueError("tool_execution_update requires ToolProgress")


class AgentRunState(StrEnum):
    """The complete state space for one Agent Run."""

    ACTIVE = "active"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        """Whether the state is final for the run."""

        return self is not AgentRunState.ACTIVE


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """The final, awaitable result of an Agent Run."""

    run_id: str
    state: AgentRunState
    message: AssistantMessage | None = None
    error: AgentError | None = None

    def __post_init__(self) -> None:
        if not self.state.terminal:
            raise ValueError("an AgentRunResult must be terminal")
        if self.state is AgentRunState.SETTLED and self.message is None:
            raise ValueError("a settled result requires an authoritative message")
        if self.state is AgentRunState.FAILED and self.error is None:
            raise ValueError("a failed result requires a structured error")


class AgentSessionEventKind(StrEnum):
    """Public Event Stream kinds exposed by AgentRun."""

    AGENT_START = "agent_start"
    TURN_START = "turn_start"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_UPDATE = "tool_execution_update"
    TOOL_EXECUTION_END = "tool_execution_end"
    ERROR = "error"
    TURN_END = "turn_end"
    AGENT_END = "agent_end"
    RUN_SETTLED = "run_settled"
    RUN_CANCELLED = "run_cancelled"
    RUN_FAILED = "run_failed"


_TERMINAL_EVENT_KINDS = {
    AgentSessionEventKind.RUN_SETTLED,
    AgentSessionEventKind.RUN_CANCELLED,
    AgentSessionEventKind.RUN_FAILED,
}


@dataclass(frozen=True, slots=True)
class AgentSessionEvent:
    """A public event wrapping AgentLoop activity or a run terminal state."""

    kind: AgentSessionEventKind
    run_id: str
    agent_event: AgentEvent | None = None
    result: AgentRunResult | None = None

    def __post_init__(self) -> None:
        terminal = self.kind in _TERMINAL_EVENT_KINDS
        if terminal != (self.result is not None):
            raise ValueError("terminal events require only a final result")
        if not terminal and self.agent_event is None:
            raise ValueError("non-terminal events require an AgentEvent")

    @classmethod
    def from_agent_event(cls, event: AgentEvent) -> AgentSessionEvent:
        """Lift a low-level AgentEvent into the public Event Stream."""

        return cls(
            kind=AgentSessionEventKind(event.kind.value),
            run_id=event.run_id,
            agent_event=event,
        )

    @classmethod
    def from_result(cls, result: AgentRunResult) -> AgentSessionEvent:
        """Create the single terminal event for a final result."""

        kind = {
            AgentRunState.SETTLED: AgentSessionEventKind.RUN_SETTLED,
            AgentRunState.CANCELLED: AgentSessionEventKind.RUN_CANCELLED,
            AgentRunState.FAILED: AgentSessionEventKind.RUN_FAILED,
        }[result.state]
        return cls(kind=kind, run_id=result.run_id, result=result)

    @property
    def message(self) -> AssistantMessage | None:
        """Expose the cumulative or authoritative message, when present."""

        return None if self.agent_event is None else self.agent_event.message

    @property
    def provider_event(self) -> ProviderStreamEvent | None:
        """Expose the raw provider event attached to a message update."""

        return None if self.agent_event is None else self.agent_event.provider_event

    @property
    def error(self) -> AgentError | None:
        """Expose a structured low-level or terminal error, when present."""

        if self.agent_event is not None:
            return self.agent_event.error
        return None if self.result is None else self.result.error

    @property
    def tool_call(self) -> ToolCall | None:
        return None if self.agent_event is None else self.agent_event.tool_call

    @property
    def tool_result(self) -> ToolResult | None:
        return None if self.agent_event is None else self.agent_event.tool_result

    @property
    def tool_progress(self) -> ToolProgress | None:
        return None if self.agent_event is None else self.agent_event.tool_progress
