"""ModelProvider seam and deterministic scripted Fake Provider."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, TypeVar, cast

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


@dataclass(frozen=True, slots=True)
class ContextResource:
    """A current authoritative resource re-projected for one Provider request."""

    source: str
    authority: Literal["kernel", "runtime", "project", "extension"]
    resource_id: str
    revision: str
    content: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.source, self.resource_id, self.revision, self.content)
        ):
            raise ValueError("ContextResource fields must be non-empty strings")
        if self.authority not in {"kernel", "runtime", "project", "extension"}:
            raise ValueError("ContextResource authority is invalid")


ModelMessage: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage | BranchSummaryMessage


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, object], ...] = ()
    system_prompt: str = ""
    tool_guidelines: str = ""
    project_context: tuple[str, ...] = ()
    resources: tuple[ContextResource, ...] = ()


class ModelProvider(Protocol):
    """A provider that normalizes one response into stream events."""

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        """Stream one scripted or model-backed response."""
        ...


class ProviderOwnedCancellationError(RuntimeError):
    """A Provider cancelled its child task rather than the authoritative Host task."""


T = TypeVar("T")


async def isolated_provider_operation(
    operation: Callable[[], Awaitable[T]],
    *,
    cancellation_message: str,
) -> T:
    """Run one Provider-owned awaitable without adopting Provider-owned cancellation."""

    async def invoke() -> T:
        return await operation()

    worker = asyncio.create_task(invoke())
    try:
        return await worker
    except asyncio.CancelledError as exc:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        raise ProviderOwnedCancellationError(cancellation_message) from exc
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def isolated_provider_events(
    stream: AsyncIterator[object],
) -> AsyncIterator[object]:
    """Read Provider events in child tasks while preserving Host cancellation."""

    while True:

        async def read_one() -> object:
            return await anext(stream)

        try:
            yield await isolated_provider_operation(
                read_one,
                cancellation_message="Provider cancelled its own stream task",
            )
        except StopAsyncIteration:
            return


async def isolated_provider_close(close: Callable[[], object]) -> None:
    """Close a Provider stream without adopting cleanup cancellation."""

    async def close_one() -> None:
        result = close()
        if not inspect.isawaitable(result):
            raise TypeError("Provider stream aclose must be awaitable")
        await cast(Awaitable[object], result)

    await isolated_provider_operation(
        close_one,
        cancellation_message="Provider cancelled its own cleanup task",
    )


async def isolated_provider_factory(
    provider: ModelProvider,
    request: ProviderRequest,
) -> object:
    """Call a Provider stream factory in a cancellation-isolated child task."""

    async def create_provider_stream() -> object:
        return provider.stream(request)

    return await isolated_provider_operation(
        create_provider_stream,
        cancellation_message="Provider stream factory attempted cancellation",
    )


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
