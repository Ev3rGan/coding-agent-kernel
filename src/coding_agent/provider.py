"""ModelProvider seam and deterministic scripted Fake Provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from coding_agent.events import (
    AssistantMessage,
    ProviderDone,
    ProviderError,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class UserMessage:
    role: Literal["user"] = "user"
    text: str = ""


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    role: Literal["tool"] = "tool"
    results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class BranchSummaryMessage:
    """A provider-neutral summary of an older Active Branch prefix."""

    role: Literal["summary"] = "summary"
    text: str = ""


ModelMessage: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage | BranchSummaryMessage


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, object], ...] = ()
    system_prompt: str = ""
    tool_guidelines: str = ""
    project_context: tuple[str, ...] = ()


class ModelProvider(Protocol):
    """A provider that normalizes one response into stream events."""

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        """Stream one scripted or model-backed response."""
        ...


class FakeProvider:
    """A deterministic provider backed by an immutable event script."""

    def __init__(
        self,
        script: Iterable[ProviderStreamEvent] | Iterable[Sequence[ProviderStreamEvent]],
    ) -> None:
        items = tuple(script)
        if items and isinstance(items[0], Sequence):
            self._scripts = tuple(tuple(turn) for turn in items)  # type: ignore[arg-type]
        else:
            self._scripts = (tuple(items),)  # type: ignore[arg-type]
        self._request_index = 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        """Yield the configured script with an async scheduling boundary per event."""

        self.requests.append(request)
        index = min(self._request_index, len(self._scripts) - 1)
        self._request_index += 1
        for event in self._scripts[index]:
            await asyncio.sleep(0)
            yield event

    @classmethod
    def streamed_run(cls) -> FakeProvider:
        """Create the successful demo script."""

        script: tuple[ProviderStreamEvent, ...] = (
            ProviderThinkingDelta("Plan the response. "),
            ProviderThinkingDelta("Then answer."),
            ProviderTextDelta("Hello"),
            ProviderTextDelta(" from the"),
            ProviderTextDelta(" Fake Provider."),
            ProviderDone(),
        )
        return cls(script)

    @classmethod
    def provider_error(cls) -> FakeProvider:
        """Create the demo script that terminates with a provider failure."""

        script: tuple[ProviderStreamEvent, ...] = (
            ProviderThinkingDelta("Begin the response."),
            ProviderTextDelta("Partial output"),
            ProviderError(
                code="scripted_provider_error",
                message="The scripted provider failed.",
            ),
        )
        return cls(script)
