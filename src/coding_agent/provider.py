"""ModelProvider seam and deterministic scripted Fake Provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Protocol

from coding_agent.events import (
    ProviderDone,
    ProviderError,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderThinkingDelta,
)


class ModelProvider(Protocol):
    """A provider that normalizes one response into stream events."""

    def stream(self, prompt: str) -> AsyncIterator[ProviderStreamEvent]:
        """Stream one scripted or model-backed response."""
        ...


class FakeProvider:
    """A deterministic provider backed by an immutable event script."""

    def __init__(self, script: Iterable[ProviderStreamEvent]) -> None:
        self._script = tuple(script)

    async def stream(self, prompt: str) -> AsyncIterator[ProviderStreamEvent]:
        """Yield the configured script with an async scheduling boundary per event."""

        del prompt
        for event in self._script:
            await asyncio.sleep(0)
            yield event

    @classmethod
    def streamed_run(cls) -> FakeProvider:
        """Create the successful demo script."""

        return cls(
            [
                ProviderThinkingDelta("Plan the response. "),
                ProviderThinkingDelta("Then answer."),
                ProviderTextDelta("Hello"),
                ProviderTextDelta(" from the"),
                ProviderTextDelta(" Fake Provider."),
                ProviderDone(),
            ]
        )

    @classmethod
    def provider_error(cls) -> FakeProvider:
        """Create the demo script that terminates with a provider failure."""

        return cls(
            [
                ProviderThinkingDelta("Begin the response."),
                ProviderTextDelta("Partial output"),
                ProviderError(
                    code="scripted_provider_error",
                    message="The scripted provider failed.",
                ),
            ]
        )
