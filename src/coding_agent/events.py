"""Typed event and result values crossing the Kernel's public seams."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TypeAlias


class ProviderEventKind(StrEnum):
    """Normalized kinds emitted by a streaming model provider."""

    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    """A text fragment from the provider."""

    delta: str
    kind: Literal[ProviderEventKind.TEXT_DELTA] = field(
        default=ProviderEventKind.TEXT_DELTA,
        init=False,
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
class ProviderDone:
    """The provider's authoritative successful stream terminator."""

    stop_reason: str = "stop"
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


ProviderStreamEvent: TypeAlias = (
    ProviderTextDelta | ProviderThinkingDelta | ProviderDone | ProviderError
)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """The cumulative assistant message assembled from provider increments."""

    role: Literal["assistant"] = "assistant"
    text: str = ""
    thinking: str = ""

    def apply(self, event: ProviderStreamEvent) -> AssistantMessage:
        """Return the cumulative message after applying one provider event."""

        if isinstance(event, ProviderTextDelta):
            return AssistantMessage(text=self.text + event.delta, thinking=self.thinking)
        if isinstance(event, ProviderThinkingDelta):
            return AssistantMessage(text=self.text, thinking=self.thinking + event.delta)
        return self


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

    def __post_init__(self) -> None:
        if self.kind is AgentEventKind.MESSAGE_UPDATE:
            if self.message is None or self.provider_event is None:
                raise ValueError("message_update requires a message and provider_event")
        elif self.kind is AgentEventKind.MESSAGE_END and self.message is None:
            raise ValueError("message_end requires the authoritative message")
        elif self.kind is AgentEventKind.ERROR and self.error is None:
            raise ValueError("error requires a structured AgentError")


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
