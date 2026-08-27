from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent import (
    AgentKernel,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    FakeProvider,
    InMemorySessionStore,
    LocalCodingEnvironment,
    PendingMessageKind,
    ProviderDone,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    RetryPolicy,
    ToolOutput,
    ToolResultMessage,
    ToolRuntime,
    ToolSpec,
)


class ReleasableProvider:
    """Deterministic Provider boundary that exposes each request start."""

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        yield ProviderTextDelta(f"answer-{len(self.requests)}")
        yield ProviderDone()


class BlockingTool:
    spec = ToolSpec(
        name="blocking",
        description="Wait for a deterministic Host release.",
        schema={"type": "object", "required": [], "properties": {}},
        mode="sequential",
        enabled_by_default=False,
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: object,
    ) -> ToolOutput:
        del arguments, environment, on_progress
        self.started.set()
        release_task = asyncio.create_task(self.release.wait())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        waiters = {release_task} if cancel_task is None else {release_task, cancel_task}
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task is not None and cancel_task in done:
                self.cancel_seen.set()
                raise asyncio.CancelledError
            return ToolOutput({"content": "tool complete"})
        finally:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_seen.set()
            for task in waiters:
                if not task.done():
                    task.cancel()


class StubbornParallelTool:
    """A Tool that proves ToolRuntime cancels children, not only its batch task."""

    spec = ToolSpec(
        name="stubborn",
        description="Wait until the task itself is cancelled.",
        schema={"type": "object", "required": [], "properties": {}},
        mode="parallel",
        enabled_by_default=False,
    )

    def __init__(self) -> None:
        self.active = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: object,
    ) -> ToolOutput:
        del arguments, environment, cancel_event, on_progress
        self.active += 1
        if self.active == 2:
            self.both_started.set()
        try:
            await self.release.wait()
            return ToolOutput({"content": "released"})
        finally:
            self.active -= 1


def _tool_call_script() -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCallStart(index=0),
        ProviderToolCallDelta(
            index=0,
            call_id_delta="blocking-1",
            tool_name_delta="blocking",
            arguments_delta=json.dumps({}),
        ),
        ProviderToolCallEnd(index=0),
        ProviderDone(stop_reason="tool_use"),
    )


async def _collect(run: object) -> list[AgentSessionEvent]:
    events: list[AgentSessionEvent] = []
    async for event in run:  # type: ignore[attr-defined]
        events.append(event)
    return events


def test_steering_waits_for_authoritative_provider_message_then_injects_once() -> None:
    async def scenario() -> None:
        provider = ReleasableProvider()
        store = InMemorySessionStore()
        ids = iter(("root", "prompt", "first-answer", "steering", "second-answer"))
        kernel = AgentKernel.with_new_session(
            provider,
            store,
            configuration={"provider": "releasable"},
            session_id="controlled",
            entry_id_factory=lambda: next(ids),
        )
        run = kernel.create_run("initial work")
        events_task = asyncio.create_task(_collect(run))
        await provider.started.wait()

        steering = await run.steer("change direction")

        assert steering.text == "change direction"
        assert [entry.payload.get("text") for entry in kernel.session_active_branch] == [
            None,
            "initial work",
        ]
        assert len(provider.requests) == 1

        provider.release.set()
        events = await events_task
        result = await run.result()

        assert result.state is AgentRunState.SETTLED
        assert len(provider.requests) == 2
        assert [getattr(message, "text", None) for message in provider.requests[1].messages] == [
            "initial work",
            "answer-1",
            "change direction",
        ]
        assert [entry.payload.get("text") for entry in kernel.session_active_branch] == [
            None,
            "initial work",
            "answer-1",
            "change direction",
            "answer-2",
        ]
        kinds = [event.kind for event in events]
        assert kinds.index(AgentSessionEventKind.MESSAGE_QUEUED) < kinds.index(
            AgentSessionEventKind.MESSAGE_END
        )
        assert kinds.index(AgentSessionEventKind.MESSAGE_END) < kinds.index(
            AgentSessionEventKind.MESSAGE_INJECTED
        )
        assert kinds.count(AgentSessionEventKind.RUN_SETTLED) == 1

    asyncio.run(scenario())


def test_control_messages_reject_blank_input() -> None:
    async def scenario() -> None:
        provider = ReleasableProvider()
        run = AgentKernel(provider).create_run("work")
        await provider.started.wait()
        with pytest.raises(ValueError, match="must not be blank"):
            await run.steer("  \n")
        with pytest.raises(ValueError, match="must not be blank"):
            await run.follow_up("")
        await run.cancel()

    asyncio.run(scenario())


def test_steering_precedes_follow_up_and_follow_up_defers_terminal_result() -> None:
    async def scenario() -> None:
        provider = ReleasableProvider()
        run = AgentKernel(provider).create_run("initial work")
        events_task = asyncio.create_task(_collect(run))
        await provider.started.wait()

        first_steering = await run.steer("steer one")
        second_steering = await run.steer("steer two")
        first_follow_up = await run.follow_up("follow one")
        second_follow_up = await run.follow_up("follow two")
        provider.release.set()

        events = await events_task
        result = await run.wait()
        injected = [
            event.pending_message
            for event in events
            if event.kind is AgentSessionEventKind.MESSAGE_INJECTED
        ]

        assert injected == [
            first_steering,
            second_steering,
            first_follow_up,
            second_follow_up,
        ]
        assert [item.kind for item in injected if item is not None] == [
            PendingMessageKind.STEERING,
            PendingMessageKind.STEERING,
            PendingMessageKind.FOLLOW_UP,
            PendingMessageKind.FOLLOW_UP,
        ]
        assert len(provider.requests) == 3
        assert [getattr(message, "text", None) for message in provider.requests[1].messages][
            -2:
        ] == [
            "steer one",
            "steer two",
        ]
        assert [getattr(message, "text", None) for message in provider.requests[2].messages][
            -2:
        ] == [
            "follow one",
            "follow two",
        ]
        terminal_indices = [
            index
            for index, event in enumerate(events)
            if event.kind
            in {
                AgentSessionEventKind.RUN_SETTLED,
                AgentSessionEventKind.RUN_CANCELLED,
                AgentSessionEventKind.RUN_FAILED,
            }
        ]
        assert terminal_indices == [len(events) - 1]
        assert result.state is AgentRunState.SETTLED

        with pytest.raises(RuntimeError, match="not active"):
            await run.follow_up("too late")

    asyncio.run(scenario())


def test_steering_during_tool_batch_injects_after_tool_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        tool = BlockingTool()
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        runtime.register(tool)
        runtime.enable("blocking")
        fake = FakeProvider([_tool_call_script(), (ProviderTextDelta("finished"), ProviderDone())])
        store = InMemorySessionStore()
        kernel = AgentKernel.with_new_session(
            fake,
            store,
            configuration={"provider": "fake"},
            tool_runtime=runtime,
        )
        run = kernel.create_run("use the tool")
        events_task = asyncio.create_task(_collect(run))
        await tool.started.wait()

        steering = await run.steer("inspect the result")
        assert all(
            entry.payload.get("text") != "inspect the result"
            for entry in kernel.session_active_branch
        )
        tool.release.set()

        events = await events_task
        assert (await run.result()).state is AgentRunState.SETTLED
        kinds = [event.kind for event in events]
        assert kinds.index(AgentSessionEventKind.TOOL_EXECUTION_END) < kinds.index(
            AgentSessionEventKind.MESSAGE_INJECTED
        )
        assert (
            next(
                event.pending_message
                for event in events
                if event.kind is AgentSessionEventKind.MESSAGE_INJECTED
            )
            == steering
        )
        assert len(fake.requests) == 2
        assert isinstance(fake.requests[1].messages[-2], ToolResultMessage)
        assert getattr(fake.requests[1].messages[-1], "text", None) == "inspect the result"
        persisted = [entry.payload.get("text") for entry in kernel.session_active_branch]
        assert persisted.count("inspect the result") == 1

    asyncio.run(scenario())


def test_retryable_provider_failure_reuses_context_without_persisting_partial_attempt() -> None:
    async def scenario() -> None:
        scripts: tuple[tuple[ProviderStreamEvent, ...], ...] = (
            (
                ProviderTextDelta("discarded partial"),
                ProviderError("provider_unavailable", "temporary"),
            ),
            (ProviderTextDelta("recovered"), ProviderDone()),
        )
        provider = FakeProvider(scripts)
        store = InMemorySessionStore()
        kernel = AgentKernel.with_new_session(
            provider,
            store,
            configuration={"provider": "fake"},
            retry_policy=RetryPolicy(max_attempts=2),
        )
        run = kernel.create_run("retry this")
        events = await _collect(run)
        result = await run.result()

        assert result.state is AgentRunState.SETTLED
        assert provider.requests[0] is provider.requests[1]
        retries = [event for event in events if event.kind is AgentSessionEventKind.PROVIDER_RETRY]
        assert [(event.retry_attempt, event.retry_remaining) for event in retries] == [(1, 1)]
        assert retries[0].retry_error is not None
        assert retries[0].retry_error.code == "provider_unavailable"
        assert sum(event.kind is AgentSessionEventKind.MESSAGE_END for event in events) == 1
        persisted = [entry.payload.get("text") for entry in kernel.session_active_branch]
        assert "discarded partial" not in persisted
        assert persisted.count("recovered") == 1

    asyncio.run(scenario())


def test_retry_exhaustion_and_non_retryable_failure_each_have_one_failed_terminal() -> None:
    async def exercise(code: str, max_attempts: int) -> tuple[list[AgentSessionEvent], int]:
        provider = FakeProvider([(ProviderError(code, "failed"),)])
        run = AgentKernel(
            provider,
            retry_policy=RetryPolicy(max_attempts=max_attempts),
        ).create_run("work")
        events = await _collect(run)
        result = await run.result()
        assert result.state is AgentRunState.FAILED
        assert sum(event.kind is AgentSessionEventKind.RUN_FAILED for event in events) == 1
        return events, len(provider.requests)

    async def scenario() -> None:
        exhausted, exhausted_requests = await exercise("provider_unavailable", 3)
        non_retryable, non_retryable_requests = await exercise("invalid_request", 3)

        assert exhausted_requests == 3
        assert [
            (event.retry_attempt, event.retry_remaining)
            for event in exhausted
            if event.kind is AgentSessionEventKind.PROVIDER_RETRY
        ] == [(1, 2), (2, 1)]
        assert non_retryable_requests == 1
        assert all(
            event.kind is not AgentSessionEventKind.PROVIDER_RETRY for event in non_retryable
        )

    asyncio.run(scenario())


def test_cancel_during_retry_wait_produces_only_cancelled_terminal() -> None:
    async def scenario() -> None:
        provider = FakeProvider([(ProviderError("provider_unavailable", "temporary"),)])
        run = AgentKernel(
            provider,
            retry_policy=RetryPolicy(max_attempts=3, delay_seconds=3600),
        ).create_run("work")
        retry_observed = asyncio.Event()
        events: list[AgentSessionEvent] = []

        async def observe() -> None:
            async for event in run:
                events.append(event)
                if event.kind is AgentSessionEventKind.PROVIDER_RETRY:
                    retry_observed.set()

        observer = asyncio.create_task(observe())
        await retry_observed.wait()
        result = await run.cancel()
        await observer

        assert result.state is AgentRunState.CANCELLED
        assert len(provider.requests) == 1
        assert [
            event.kind
            for event in events
            if event.kind
            in {
                AgentSessionEventKind.RUN_SETTLED,
                AgentSessionEventKind.RUN_CANCELLED,
                AgentSessionEventKind.RUN_FAILED,
            }
        ] == [AgentSessionEventKind.RUN_CANCELLED]

    asyncio.run(scenario())


def test_concurrent_cancel_result_and_wait_drop_pending_without_session_persistence() -> None:
    async def scenario() -> None:
        provider = ReleasableProvider()
        store = InMemorySessionStore()
        kernel = AgentKernel.with_new_session(
            provider,
            store,
            configuration={"provider": "releasable"},
        )
        run = kernel.create_run("initial work")
        events_task = asyncio.create_task(_collect(run))
        await provider.started.wait()
        steering = await run.steer("queued steering")
        follow_up = await run.follow_up("queued follow-up")

        results = await asyncio.gather(
            run.cancel(),
            run.cancel(),
            run.result(),
            run.wait(),
        )
        events = await events_task

        assert all(result == results[0] for result in results)
        assert results[0].state is AgentRunState.CANCELLED
        assert [
            event.pending_message
            for event in events
            if event.kind is AgentSessionEventKind.MESSAGE_DROPPED
        ] == [steering, follow_up]
        assert all(event.kind is not AgentSessionEventKind.MESSAGE_INJECTED for event in events)
        assert sum(event.kind is AgentSessionEventKind.RUN_CANCELLED for event in events) == 1
        assert [entry.payload.get("text") for entry in kernel.session_active_branch] == [
            None,
            "initial work",
        ]
        assert len(provider.requests) == 1

        with pytest.raises(RuntimeError, match="not active"):
            await run.steer("too late")

    asyncio.run(scenario())


def test_cancel_propagates_shared_signal_to_in_flight_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        tool = BlockingTool()
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        runtime.register(tool)
        runtime.enable("blocking")
        provider = FakeProvider([_tool_call_script()])
        run = AgentKernel(provider, tool_runtime=runtime).create_run("use tool")
        events_task = asyncio.create_task(_collect(run))
        await tool.started.wait()

        result = await run.cancel()
        events = await events_task

        assert result.state is AgentRunState.CANCELLED
        assert tool.cancel_seen.is_set()
        assert len(provider.requests) == 1
        assert sum(event.kind is AgentSessionEventKind.RUN_CANCELLED for event in events) == 1
        assert all(event.kind is not AgentSessionEventKind.TOOL_EXECUTION_END for event in events)

    asyncio.run(scenario())


def test_cancel_joins_parallel_tool_children_before_terminal_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        tool = StubbornParallelTool()
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        runtime.register(tool)
        runtime.enable("stubborn")
        first_turn = (
            ProviderToolCallStart(index=0),
            ProviderToolCallDelta(
                index=0,
                call_id_delta="stubborn-1",
                tool_name_delta="stubborn",
                arguments_delta="{}",
            ),
            ProviderToolCallEnd(index=0),
            ProviderToolCallStart(index=1),
            ProviderToolCallDelta(
                index=1,
                call_id_delta="stubborn-2",
                tool_name_delta="stubborn",
                arguments_delta="{}",
            ),
            ProviderToolCallEnd(index=1),
            ProviderDone("tool_use"),
        )
        run = AgentKernel(FakeProvider([first_turn]), tool_runtime=runtime).create_run(
            "parallel work"
        )
        events_task = asyncio.create_task(_collect(run))
        await tool.both_started.wait()

        result = await run.cancel()
        active_at_terminal = tool.active
        tool.release.set()
        await asyncio.sleep(0)
        await events_task

        assert result.state is AgentRunState.CANCELLED
        assert active_at_terminal == 0

    asyncio.run(scenario())
