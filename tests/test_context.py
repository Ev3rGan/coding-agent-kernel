from __future__ import annotations

import pytest

from coding_agent import (
    AgentKernel,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    ContextConstructionError,
    ContextInput,
    ContextPipeline,
    ContextSettings,
    FakeProvider,
    InMemorySessionStore,
    ProviderDone,
    Session,
    SessionEntry,
    UserMessage,
)


class _FailingSummarizer:
    def summarize(self, messages: tuple[object, ...]) -> str:
        raise RuntimeError("deterministic summary failure")


def test_context_pipeline_orders_inputs_and_excludes_sibling_and_pending_messages() -> None:
    ids = iter(("root", "sibling-user", "sibling-answer", "active-user"))
    session = Session.create(
        InMemorySessionStore(),
        session_id="session-context",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    root = session.active_leaf_id
    session.record_user_message("SIBLING_MARKER")
    session.record_authoritative_message(AssistantMessage(text="sibling answer"))
    session.fork(root)
    session.record_user_message("ACTIVE_MARKER")

    result = ContextPipeline().build(
        ContextInput(
            settings=ContextSettings(
                system_prompt="SYSTEM_MARKER",
                tool_guidelines="TOOL_GUIDELINE_MARKER",
                project_context=("PROJECT_MARKER",),
                max_characters=2_000,
            ),
            active_branch=session.active_branch,
            active_tools=(
                {
                    "name": "read",
                    "description": "Read a file",
                    "schema": {"type": "object"},
                    "mode": "parallel",
                },
            ),
            injected_messages=(UserMessage(text="INJECTED_MARKER"),),
            pending_messages=(UserMessage(text="PENDING_MARKER"),),
        )
    )

    assert result.context.assembly_order == (
        "system_prompt",
        "active_tools",
        "project_context",
        "active_branch",
        "injected_messages",
        "provider_request",
    )
    assert result.compaction is None
    assert result.context.provider_request.system_prompt == "SYSTEM_MARKER"
    assert result.context.provider_request.tool_guidelines == "TOOL_GUIDELINE_MARKER"
    assert result.context.provider_request.project_context == ("PROJECT_MARKER",)
    message_texts = [
        getattr(message, "text", None) for message in result.context.provider_request.messages
    ]
    assert message_texts == [
        "ACTIVE_MARKER",
        "INJECTED_MARKER",
    ]
    rendered = repr(result.context.provider_request)
    assert "SIBLING_MARKER" not in rendered
    assert "PENDING_MARKER" not in rendered


def test_over_budget_context_persists_a_bounded_compaction_without_deleting_history() -> None:
    ids = iter(("root", "old-user", "old-answer", "checkpoint"))
    session = Session.create(
        InMemorySessionStore(),
        session_id="session-compaction",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 80)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 80))
    original_ids = tuple(entry.entry_id for entry in session.active_branch)

    result = ContextPipeline().build(
        ContextInput(
            settings=ContextSettings(max_characters=500),
            active_branch=session.active_branch,
            injected_messages=(UserMessage(text="current request"),),
        )
    )

    assert result.compaction is not None
    checkpoint = session.record_compaction(result.compaction)
    assert checkpoint.kind == "compaction"
    assert checkpoint.payload["covered_entry_ids"] == ["old-user", "old-answer"]
    assert result.context.bounded
    assert [message.role for message in result.context.provider_request.messages] == [
        "summary",
        "user",
    ]
    assert tuple(entry.entry_id for entry in session.active_branch[:3]) == original_ids
    assert tuple(entry.entry_id for entry in session.active_branch) == (*original_ids, "checkpoint")
    assert "old question " * 20 not in repr(result.context.provider_request)


def test_kernel_uses_context_pipeline_and_emits_persisted_compaction_event() -> None:
    ids = iter(("root", "old-user", "old-answer", "checkpoint", "current", "answer"))
    session = Session.create(
        InMemorySessionStore(),
        session_id="session-kernel-context",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 80)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 80))
    session.drain_events()
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=500),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("CURRENT_INJECTED_MARKER")
        events = [event async for event in run]
        return events, await run.result()

    import asyncio

    events, result = asyncio.run(collect())

    assert result.state is AgentRunState.SETTLED
    assert len(provider.requests) == 1
    assert [message.role for message in provider.requests[0].messages] == ["summary", "user"]
    assert getattr(provider.requests[0].messages[-1], "text", None) == "CURRENT_INJECTED_MARKER"
    compaction_events = [
        event for event in events if event.kind is AgentSessionEventKind.COMPACTION_SUCCEEDED
    ]
    assert len(compaction_events) == 1
    assert compaction_events[0].session_entry is not None
    assert compaction_events[0].session_entry.entry_id == "checkpoint"
    assert any(entry.kind == "compaction" for entry in session.active_branch)


def test_summary_failure_prevents_provider_call_and_preserves_recoverable_session() -> None:
    ids = iter(("root", "old-user", "old-answer"))
    store = InMemorySessionStore()
    session = Session.create(
        store,
        session_id="session-summary-error",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 80)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 80))
    original_ids = tuple(entry.entry_id for entry in session.active_branch)
    session.drain_events()
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_pipeline=ContextPipeline(_FailingSummarizer()),
        context_settings=ContextSettings(max_characters=500),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("CURRENT_INJECTED_MARKER")
        events = [event async for event in run]
        return events, await run.result()

    import asyncio

    events, result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "compaction_summary_failed"
    assert provider.requests == []
    assert tuple(entry.entry_id for entry in session.active_branch) == original_ids
    assert all(entry.kind != "compaction" for entry in session.active_branch)
    assert AgentSessionEventKind.COMPACTION_FAILED in [event.kind for event in events]
    session.close()
    resumed = Session.resume(store, "session-summary-error")
    assert tuple(entry.entry_id for entry in resumed.active_branch) == original_ids


def test_context_budget_failure_emits_context_event_before_provider_call() -> None:
    ids = iter(("root",))
    session = Session.create(
        InMemorySessionStore(),
        session_id="session-context-error",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.drain_events()
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=1),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("cannot fit")
        events = [event async for event in run]
        return events, await run.result()

    import asyncio

    events, result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "context_budget_exceeded"
    assert provider.requests == []
    assert events[0].kind is AgentSessionEventKind.CONTEXT_FAILED
    assert tuple(entry.entry_id for entry in session.active_branch) == ("root",)


def test_context_pipeline_rejects_checkpoint_covering_a_different_branch() -> None:
    branch = (
        SessionEntry("root", "session", None, "configuration", "{}"),
        SessionEntry(
            "active-message",
            "session",
            "root",
            "message",
            '{"role":"user","text":"active"}',
        ),
        SessionEntry(
            "checkpoint",
            "session",
            "active-message",
            "compaction",
            '{"covered_entry_ids":["sibling-message"],"summary":"wrong","version":1}',
        ),
    )

    with pytest.raises(ContextConstructionError, match="checkpoint.*invalid") as raised:
        ContextPipeline().build(
            ContextInput(
                settings=ContextSettings(),
                active_branch=branch,
            )
        )

    assert raised.value.code == "compaction_checkpoint_invalid"
