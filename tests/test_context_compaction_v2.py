from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from coding_agent import (
    AgentKernel,
    AgentRunResult,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    Block,
    BranchSummaryMessage,
    CompactionDraft,
    CompactionHookInput,
    CompactionInput,
    CompactionMetrics,
    CompactionPlan,
    ContextHookInput,
    ContextInput,
    ContextPipeline,
    ContextResource,
    ContextResourceHookInput,
    ContextResourceSupplement,
    ContextSettings,
    ContextSupplement,
    ExtensionRegistry,
    FakeProvider,
    Hook,
    InMemorySessionStore,
    JsonlSessionStore,
    ModelContext,
    ProviderCompactionEngine,
    ProviderDone,
    ProviderError,
    ProviderRequest,
    ProviderRequestHookInput,
    ProviderTextDelta,
    ProviderUsage,
    Session,
    SessionEntry,
    SessionRelationError,
    Supplement,
    TokenUsage,
    ToolCall,
    ToolError,
    ToolResult,
    ToolResultMessage,
    Transform,
    UserMessage,
    estimate_provider_request_characters,
)

_SUMMARY_HEADINGS = (
    "Goal",
    "Constraints and preferences",
    "Done and verified",
    "In progress",
    "Blocked",
    "Key decisions and relevant rejected approaches",
    "Exact files, symbols, commands and errors",
    "Next steps",
)


def _summary(marker: str) -> str:
    return "\n".join(f"## {heading}\n{marker}: {heading}" for heading in _SUMMARY_HEADINGS)


class _RecordingCompactionEngine:
    def __init__(self, summaries: Iterable[str]) -> None:
        self._summaries = iter(summaries)
        self.inputs: list[CompactionInput] = []

    async def compact(self, compaction_input: CompactionInput) -> CompactionDraft:
        self.inputs.append(compaction_input)
        return CompactionDraft(next(self._summaries), TokenUsage(17, 9))


class _ClosableSummaryStream(AsyncIterator[object]):
    def __init__(self, events: Iterable[object]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> _ClosableSummaryStream:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class _ClosableSummaryProvider:
    def __init__(self, events: Iterable[object]) -> None:
        self._events = tuple(events)
        self.requests: list[ProviderRequest] = []
        self.streams: list[_ClosableSummaryStream] = []

    def stream(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        stream = _ClosableSummaryStream(self._events)
        self.streams.append(stream)
        return stream


class _SelfCancellingSummaryProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def stream(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)

        async def events() -> AsyncIterator[object]:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
            yield ProviderDone()  # pragma: no cover - self-cancellation must win

        return events()


class _SkillResourceExtension:
    name = "skill-resource"

    def __init__(self) -> None:
        self.revision = "sha256:first"
        self.content = "FIRST_SKILL_RULE"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT_RESOURCE, self._resource)

    def _resource(
        self,
        hook_input: ContextResourceHookInput,
    ) -> Supplement[ContextResourceSupplement]:
        del hook_input
        return Supplement(
            ContextResourceSupplement(
                resources=(
                    ContextResource(
                        source="skill-loader",
                        authority="extension",
                        resource_id="skill:active",
                        revision=self.revision,
                        content=self.content,
                    ),
                )
            )
        )


class _DowngradeCompactionExtension:
    name = "downgrade-compaction"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.COMPACTION_START, self._downgrade)

    def _downgrade(
        self,
        hook_input: CompactionHookInput,
    ) -> Transform[object]:
        assert hook_input.plan is not None
        return Transform(replace(hook_input.plan, version=1))


class _InvalidCompactionSummaryExtension:
    name = "invalid-compaction-summary"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.COMPACTION_START, self._rewrite)

    def _rewrite(
        self,
        hook_input: CompactionHookInput,
    ) -> Transform[object]:
        assert hook_input.plan is not None
        return Transform(replace(hook_input.plan, summary="unstructured stale summary"))


class _CompactedContextSupplementExtension:
    name = "compacted-context-supplement"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT, self._supplement)

    def _supplement(self, hook_input: ContextHookInput) -> Supplement[ContextSupplement]:
        del hook_input
        return Supplement(
            ContextSupplement(
                messages=(UserMessage(text="CURRENT_EXTENSION_ANNOTATION"),),
                project_context=("CURRENT_PROJECT_STATE",),
            )
        )


class _BlockProviderRequestExtension:
    name = "block-provider-request"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_REQUEST, self._block)

    def _block(self, hook_input: ProviderRequestHookInput) -> Block:
        del hook_input
        return Block("policy_denied", "do not call the coding Provider")


class _RewriteCheckpointSummaryExtension:
    name = "rewrite-checkpoint-summary"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT, self._rewrite)

    def _rewrite(self, hook_input: ContextHookInput) -> Transform[ModelContext]:
        request = hook_input.context.provider_request
        assert isinstance(request.messages[0], BranchSummaryMessage)
        return Transform(
            replace(
                hook_input.context,
                provider_request=replace(
                    request,
                    messages=(BranchSummaryMessage(text="forged summary"), *request.messages[1:]),
                ),
            )
        )


class _AppendCompactionMessageExtension:
    name = "append-compaction-message"

    def __init__(self, message: BranchSummaryMessage | UserMessage) -> None:
        self._message = message

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_REQUEST, self._append)

    def _append(self, hook_input: ProviderRequestHookInput) -> Transform[object]:
        return Transform(
            replace(
                hook_input.request,
                messages=(*hook_input.request.messages, self._message),
            )
        )


class _LateProviderResourceExtension:
    name = "late-provider-resource"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_REQUEST, self._add_resource)

    def _add_resource(
        self,
        hook_input: ProviderRequestHookInput,
    ) -> Transform[object]:
        return Transform(
            replace(
                hook_input.request,
                resources=(
                    *hook_input.request.resources,
                    ContextResource(
                        source="late-loader",
                        authority="extension",
                        resource_id="late-skill",
                        revision="1",
                        content="must have been loaded before compaction",
                    ),
                ),
            )
        )


class _SplitToolTransactionExtension:
    name = "split-tool-transaction"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.COMPACTION_START, self._split)

    def _split(self, hook_input: CompactionHookInput) -> Transform[object]:
        assert hook_input.plan is not None
        return Transform(
            replace(
                hook_input.plan,
                covered_entry_ids=("old-user", "old-call"),
                first_kept_entry_id="old-result",
                metrics=replace(
                    hook_input.plan.metrics,
                    covered_count=2,
                    retained_count=2,
                ),
            )
        )


class _ExpandCompactionCoverageExtension:
    name = "expand-compaction-coverage"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.COMPACTION_START, self._expand)

    def _expand(self, hook_input: CompactionHookInput) -> Transform[object]:
        assert hook_input.plan is not None
        return Transform(
            replace(
                hook_input.plan,
                covered_entry_ids=("old-user", "old-answer", "recent-user", "recent-answer"),
                summary=_summary("extension-expanded"),
                first_kept_entry_id=None,
                metrics=replace(
                    hook_input.plan.metrics,
                    covered_count=4,
                    retained_count=0,
                ),
            )
        )


def _build(pipeline: ContextPipeline, context_input: ContextInput):  # type: ignore[no-untyped-def]
    return asyncio.run(pipeline.build(context_input))


def _session_with_ids(*ids: str) -> Session:
    iterator = iter(ids)
    return Session.create(
        InMemorySessionStore(),
        session_id="context-v2",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(iterator),
    )


def _record_turn(session: Session, user: str, assistant: str) -> tuple[SessionEntry, SessionEntry]:
    return (
        session.record_user_message(user),
        session.record_authoritative_message(AssistantMessage(text=assistant)),
    )


def test_v2_compaction_keeps_a_recent_complete_turn_and_reprojects_authority() -> None:
    session = _session_with_ids(
        "root", "old-user", "old-answer", "new-user", "new-answer", "checkpoint"
    )
    _record_turn(session, "old question " * 90, "old answer " * 90)
    _record_turn(session, "current diagnosis", "current result")
    engine = _RecordingCompactionEngine((_summary("first"),))
    resource = ContextResource(
        source="skill-loader",
        authority="extension",
        resource_id="skill:repository-guidance",
        revision="sha256:current",
        content="CURRENT_SKILL_RULE",
    )

    built = _build(
        ContextPipeline(engine),
        ContextInput(
            settings=ContextSettings(max_characters=1_500, max_summary_characters=900),
            active_branch=session.active_branch,
            authoritative_resources=(resource,),
            injected_messages=(UserMessage(text="continue"),),
        ),
    )

    assert built.compaction is not None
    assert built.compaction.version == 2
    assert built.compaction.covered_entry_ids == ("old-user", "old-answer")
    assert built.compaction.first_kept_entry_id == "new-user"
    assert built.compaction.previous_checkpoint_id is None
    assert built.compaction.metrics.covered_count == 2
    assert built.compaction.metrics.retained_count == 2
    assert built.compaction.metrics.compaction_depth == 1
    assert built.compaction.metrics.summary_usage == TokenUsage(17, 9)
    assert [message.role for message in built.context.provider_request.messages] == [
        "summary",
        "user",
        "assistant",
        "user",
    ]
    assert built.context.provider_request.resources == (resource,)
    assert engine.inputs[0].authoritative_resources == (resource,)
    assert [entry.entry_id for entry in engine.inputs[0].newly_covered_entries] == [
        "old-user",
        "old-answer",
    ]
    assert "CURRENT_SKILL_RULE" not in repr(engine.inputs[0].newly_covered_entries)

    session.record_compaction(built.compaction)
    updated_resource = ContextResource(
        source="skill-loader",
        authority="extension",
        resource_id="skill:repository-guidance",
        revision="sha256:updated",
        content="UPDATED_SKILL_RULE",
    )
    replayed = _build(
        ContextPipeline(_RecordingCompactionEngine(())),
        ContextInput(
            settings=ContextSettings(max_characters=20_000),
            active_branch=session.active_branch,
            authoritative_resources=(updated_resource,),
        ),
    )
    assert replayed.context.provider_request.resources == (updated_resource,)
    assert "CURRENT_SKILL_RULE" not in repr(replayed.context.provider_request)


def test_repeated_compaction_passes_previous_checkpoint_separately_and_projects_only_latest() -> (
    None
):
    session = _session_with_ids(
        "root",
        "old-user",
        "old-answer",
        "kept-user",
        "kept-answer",
        "checkpoint-1",
        "middle-user",
        "middle-answer",
        "latest-user",
        "latest-answer",
        "checkpoint-2",
        "later-user",
        "later-answer",
        "final-user",
        "final-answer",
        "checkpoint-3",
    )
    _record_turn(session, "old question " * 90, "old answer " * 90)
    _record_turn(session, "kept diagnosis", "kept result")
    durable_facts = (
        "Fix the Django QuerySet clone regression",
        "Preserve existing queryset behavior",
        "django/db/models/query.py",
        "pytest django/tests/queries",
        "One clone assertion still fails",
        "Inspect QuerySet._chain",
    )

    def continuity_summary(update: str) -> str:
        return "\n".join(
            (
                f"## Goal\n{durable_facts[0]}",
                f"## Constraints and preferences\n{durable_facts[1]}",
                f"## Done and verified\nModified {durable_facts[2]}; ran {durable_facts[3]}",
                f"## In progress\n{update}",
                f"## Blocked\n{durable_facts[4]}",
                "## Key decisions and relevant rejected approaches\nAvoid unrelated ORM rewrites",
                f"## Exact files, symbols, commands and errors\n{durable_facts[2]}; "
                f"{durable_facts[3]}",
                f"## Next steps\n{durable_facts[5]}",
            )
        )

    first_summary = continuity_summary("Reproduce clone state sharing")
    second_summary = continuity_summary("Finding: clone state is shared")
    third_summary = continuity_summary("Decision: copy clone state")
    engine = _RecordingCompactionEngine((first_summary, second_summary, third_summary))
    pipeline = ContextPipeline(engine)
    settings = ContextSettings(max_characters=1_500, max_summary_characters=900)

    first = _build(
        pipeline,
        ContextInput(settings=settings, active_branch=session.active_branch),
    )
    assert first.compaction is not None
    first_checkpoint = session.record_compaction(first.compaction)
    _record_turn(session, "middle investigation " * 80, "middle finding " * 80)
    _record_turn(session, "latest diagnosis", "latest result")

    second = _build(
        pipeline,
        ContextInput(settings=settings, active_branch=session.active_branch),
    )

    assert second.compaction is not None
    assert second.compaction.covered_entry_ids == (
        "old-user",
        "old-answer",
        "kept-user",
        "kept-answer",
        "middle-user",
        "middle-answer",
    )
    assert second.compaction.first_kept_entry_id == "latest-user"
    assert second.compaction.previous_checkpoint_id == first_checkpoint.entry_id
    assert second.compaction.metrics.compaction_depth == 2
    second_input = engine.inputs[1]
    assert second_input.previous_checkpoint is not None
    assert second_input.previous_checkpoint.entry_id == "checkpoint-1"
    assert second_input.previous_checkpoint.summary == first_summary
    assert [entry.entry_id for entry in second_input.newly_covered_entries] == [
        "kept-user",
        "kept-answer",
        "middle-user",
        "middle-answer",
    ]
    assert all(entry.kind != "compaction" for entry in second_input.newly_covered_entries)
    assert "summary:summary:" not in repr(second_input.newly_covered_entries)

    session.record_compaction(second.compaction)
    replayed = _build(
        ContextPipeline(_RecordingCompactionEngine(())),
        ContextInput(
            settings=ContextSettings(max_characters=20_000),
            active_branch=session.active_branch,
        ),
    )
    summaries = [
        message
        for message in replayed.context.provider_request.messages
        if message.role == "summary"
    ]
    assert len(summaries) == 1
    assert getattr(summaries[0], "text", "") == second_summary
    for fact in durable_facts:
        assert fact in getattr(summaries[0], "text", "")
    assert [
        getattr(message, "text", None) for message in replayed.context.provider_request.messages[1:]
    ] == [
        "latest diagnosis",
        "latest result",
    ]

    _record_turn(session, "later investigation " * 80, "later result " * 80)
    _record_turn(session, "final diagnosis", "final result")
    third = _build(
        pipeline,
        ContextInput(settings=settings, active_branch=session.active_branch),
    )
    assert third.compaction is not None
    assert third.compaction.previous_checkpoint_id == "checkpoint-2"
    assert third.compaction.metrics.compaction_depth == 3
    assert third.compaction.first_kept_entry_id == "final-user"
    assert engine.inputs[2].previous_checkpoint is not None
    assert engine.inputs[2].previous_checkpoint.summary == second_summary
    assert [entry.entry_id for entry in engine.inputs[2].newly_covered_entries] == [
        "latest-user",
        "latest-answer",
        "later-user",
        "later-answer",
    ]
    assert "summary:summary:" not in repr(engine.inputs[2].newly_covered_entries)
    session.record_compaction(third.compaction)
    final_projection = _build(
        ContextPipeline(_RecordingCompactionEngine(())),
        ContextInput(
            settings=ContextSettings(max_characters=20_000),
            active_branch=session.active_branch,
        ),
    )
    assert [message.role for message in final_projection.context.provider_request.messages] == [
        "summary",
        "user",
        "assistant",
    ]
    final_summary = getattr(final_projection.context.provider_request.messages[0], "text", "")
    assert final_summary == third_summary
    for fact in durable_facts:
        assert fact in final_summary


def test_incremental_semantic_update_replaces_a_superseded_hypothesis() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-answer",
        "kept-user",
        "kept-answer",
        "checkpoint-1",
        "middle-user",
        "middle-answer",
        "latest-user",
        "latest-answer",
    )
    _record_turn(session, "old question " * 90, "old answer " * 90)
    _record_turn(session, "kept diagnosis", "kept result")
    first_summary = _summary("cache invalidation bug")
    second_summary = _summary("parser state is wrong")
    provider = FakeProvider(
        (
            (ProviderTextDelta(first_summary), ProviderDone()),
            (ProviderTextDelta(second_summary), ProviderDone()),
        )
    )
    pipeline = ContextPipeline(ProviderCompactionEngine(provider))
    settings = ContextSettings(max_characters=1_500, max_summary_characters=900)

    first = _build(
        pipeline,
        ContextInput(settings=settings, active_branch=session.active_branch),
    )
    assert first.compaction is not None
    session.record_compaction(first.compaction)
    _record_turn(
        session,
        "New test evidence disproves the cache hypothesis " * 70,
        "The parser state is the actual fault " * 70,
    )
    _record_turn(session, "update the diagnosis", "prepare the parser patch")

    second = _build(
        pipeline,
        ContextInput(settings=settings, active_branch=session.active_branch),
    )

    assert second.compaction is not None
    assert "cache invalidation bug" not in second.compaction.summary
    assert "parser state is wrong" in second.compaction.summary
    second_prompt = getattr(provider.requests[1].messages[0], "text", "")
    assert "cache invalidation bug" in second_prompt
    assert "disproves the cache hypothesis" in second_prompt


def test_safe_cut_never_splits_tool_transaction_and_sheds_only_regenerable_output() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-call",
        "old-result",
        "old-answer",
        "latest-user",
        "latest-call",
        "latest-result",
        "latest-answer",
    )
    session.record_user_message("inspect old failure")
    session.record_authoritative_message(
        AssistantMessage(
            tool_calls=(ToolCall("call-old", "shell", {"command": "pytest old", "cwd": "/repo"}),)
        )
    )
    session.record_tool_result(
        ToolResult(
            "call-old",
            "shell",
            "error",
            {"exit_code": 1, "stdout": "x" * 2_000},
            ToolError("tests_failed", "old failure"),
        )
    )
    session.record_authoritative_message(AssistantMessage(text="old blocker recorded " * 120))
    session.record_user_message("inspect latest failure")
    session.record_authoritative_message(
        AssistantMessage(
            tool_calls=(
                ToolCall(
                    "call-latest",
                    "shell",
                    {"command": "pytest latest", "cwd": "/repo"},
                ),
            )
        )
    )
    session.record_tool_result(
        ToolResult(
            "call-latest",
            "shell",
            "error",
            {
                "exit_code": 1,
                "stdout": "regenerable output " * 500,
            },
            ToolError("tests_failed", "latest failure"),
        )
    )
    session.record_authoritative_message(AssistantMessage(text="latest blocker recorded"))
    engine = _RecordingCompactionEngine((_summary("tools"),))

    built = _build(
        ContextPipeline(engine),
        ContextInput(
            settings=ContextSettings(max_characters=2_200, max_summary_characters=900),
            active_branch=session.active_branch,
            injected_messages=(UserMessage(text="continue"),),
        ),
    )

    assert built.compaction is not None
    assert built.compaction.covered_entry_ids == (
        "old-user",
        "old-call",
        "old-result",
        "old-answer",
    )
    assert built.compaction.first_kept_entry_id == "latest-user"
    assert built.compaction.evidence.commands[0].command == "pytest old"
    assert built.compaction.evidence.commands[0].cwd == "/repo"
    assert built.compaction.evidence.commands[0].exit_status == 1
    assert built.compaction.evidence.tool_errors[0].code == "tests_failed"
    retained_tool_message = next(
        message
        for message in built.context.provider_request.messages
        if isinstance(message, ToolResultMessage)
    )
    retained_output = retained_tool_message.results[0].output
    assert retained_output is not None
    assert retained_output["compaction_shed"] is True
    assert retained_output["command"] == "pytest latest"
    assert retained_output["cwd"] == "/repo"
    assert retained_output["exit_code"] == 1
    assert 0 < retained_output["stdout_excerpt"].count("regenerable output") < 20
    assert len(json.dumps(retained_output)) < 700


def test_compaction_reduces_retained_window_when_json_escaped_summary_exceeds_budget() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-answer",
        "recent-1-user",
        "recent-1-answer",
        "recent-2-user",
        "recent-2-answer",
        "recent-3-user",
        "recent-3-answer",
    )
    _record_turn(session, "old investigation " * 100, "old evidence " * 100)
    _record_turn(session, "recent one " * 10, "recent result one " * 10)
    _record_turn(session, "recent two " * 10, "recent result two " * 10)
    _record_turn(session, "recent three " * 10, "recent result three " * 10)
    base_summary = "\n".join(f"## {heading}\n-" for heading in _SUMMARY_HEADINGS)
    newline_heavy_summary = base_summary + ("\n" * 180)
    engine = _RecordingCompactionEngine((newline_heavy_summary,) * 4)
    pipeline = ContextPipeline(engine)

    built = _build(
        pipeline,
        ContextInput(
            settings=ContextSettings(max_characters=1_200, max_summary_characters=500),
            active_branch=session.active_branch,
        ),
    )

    assert built.context.bounded is True
    assert built.compaction is not None
    assert built.compaction.first_kept_entry_id is None
    assert built.compaction.metrics.retained_count == 0
    assert len(engine.inputs) == 2
    assert built.compaction.metrics.summary_usage == TokenUsage(34, 18)


def test_bounded_repeated_compaction_does_not_fail_for_low_size_reduction() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-answer",
        "checkpoint",
        "new-user",
        "new-answer",
        "checkpoint-2",
    )
    _record_turn(session, "old question", "old answer")
    base_summary = "\n".join(f"## {heading}\n-" for heading in _SUMMARY_HEADINGS)
    previous_summary = base_summary + ("p" * (500 - len(base_summary)))
    session.record_compaction(
        CompactionPlan(
            covered_entry_ids=("old-user", "old-answer"),
            summary=previous_summary,
            first_kept_entry_id=None,
            metrics=CompactionMetrics(
                characters_before=2_000,
                characters_after=645,
                covered_count=2,
                retained_count=0,
                compaction_depth=1,
            ),
        )
    )
    _record_turn(session, "n" * 180, "r" * 180)
    current_summary = base_summary + ("\\" * (600 - len(base_summary)))

    built = _build(
        ContextPipeline(_RecordingCompactionEngine((current_summary,))),
        ContextInput(
            settings=ContextSettings(max_characters=1_220, max_summary_characters=600),
            active_branch=session.active_branch,
        ),
    )

    assert built.context.bounded is True
    assert built.compaction is not None
    assert built.compaction.previous_checkpoint_id == "checkpoint"
    assert built.compaction.metrics.characters_after <= 1_220
    assert built.compaction.metrics.thrashing_detected is True
    invalid = replace(
        built.compaction,
        metrics=replace(built.compaction.metrics, thrashing_detected=False),
    )
    with pytest.raises(SessionRelationError, match="lifecycle metrics"):
        session.record_compaction(invalid)

    checkpoint = session.record_compaction(built.compaction)
    metrics = checkpoint.payload["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["thrashing_detected"] is True


def test_first_compaction_rejects_forged_thrashing_metric() -> None:
    session = _session_with_ids("root", "user", "answer", "checkpoint")
    _record_turn(session, "question", "answer")
    plan = CompactionPlan(
        covered_entry_ids=("user", "answer"),
        summary=_summary("first"),
        first_kept_entry_id=None,
        metrics=CompactionMetrics(
            characters_before=1_000,
            characters_after=990,
            covered_count=2,
            retained_count=0,
            compaction_depth=1,
            thrashing_detected=True,
        ),
    )

    with pytest.raises(SessionRelationError, match="lifecycle metrics"):
        session.record_compaction(plan)


def test_single_oversized_tool_turn_is_shed_without_an_empty_compaction_checkpoint() -> None:
    session = _session_with_ids("root", "user", "call", "result", "answer")
    session.record_user_message("inspect the only failing turn")
    session.record_authoritative_message(
        AssistantMessage(
            tool_calls=(ToolCall("only-call", "shell", {"command": "pytest only", "cwd": "/repo"}),)
        )
    )
    session.record_tool_result(
        ToolResult(
            "only-call",
            "shell",
            "error",
            {"exit_code": 1, "stdout": "regenerable output " * 500},
            ToolError("tests_failed", "the focused test failed"),
        )
    )
    session.record_authoritative_message(AssistantMessage(text="inspect the assertion next"))

    built = _build(
        ContextPipeline(_RecordingCompactionEngine(())),
        ContextInput(
            settings=ContextSettings(max_characters=1_200, max_summary_characters=600),
            active_branch=session.active_branch,
        ),
    )

    assert built.compaction is None
    assert built.context.estimated_characters <= built.context.max_characters
    tool_message = next(
        message
        for message in built.context.provider_request.messages
        if isinstance(message, ToolResultMessage)
    )
    output = tool_message.results[0].output
    assert output is not None
    assert output["compaction_shed"] is True
    assert output["exit_code"] == 1
    assert output["command"] == "pytest only"
    assert output["cwd"] == "/repo"
    assert 0 < output["stdout_excerpt"].count("regenerable output") < 20
    assert len(json.dumps(output)) < 700


def test_non_regenerable_extension_tool_output_is_not_shed_from_semantic_input() -> None:
    session = _session_with_ids("root", "user", "call", "result", "answer")
    session.record_user_message("capture the external observation")
    session.record_authoritative_message(
        AssistantMessage(
            tool_calls=(ToolCall("capture", "extension_capture", {"source": "device"}),)
        )
    )
    session.record_tool_result(
        ToolResult(
            "capture",
            "extension_capture",
            "success",
            {"observation": "NON_REGENERABLE_OBSERVATION " * 100},
        )
    )
    session.record_authoritative_message(AssistantMessage(text="preserve the observation"))
    provider = FakeProvider(((ProviderTextDelta(_summary("captured")), ProviderDone()),))

    built = _build(
        ContextPipeline(ProviderCompactionEngine(provider)),
        ContextInput(
            settings=ContextSettings(max_characters=900, max_summary_characters=500),
            active_branch=session.active_branch,
        ),
    )

    assert built.compaction is not None
    prompt = getattr(provider.requests[0].messages[0], "text", "")
    assert prompt.count("NON_REGENERABLE_OBSERVATION") == 100


def test_v1_checkpoint_replays_and_upgrades_to_v2_without_rewriting_history() -> None:
    session = _session_with_ids(
        "root",
        "legacy-user",
        "legacy-answer",
        "legacy-checkpoint",
        "new-user",
        "new-answer",
        "latest-user",
        "latest-answer",
    )
    _record_turn(session, "legacy question " * 80, "legacy answer " * 80)
    legacy_checkpoint = session.record_compaction(
        # Version 1 remains readable but is never emitted by the v2 pipeline.
        __import__("coding_agent").CompactionPlan(
            ("legacy-user", "legacy-answer"),
            "legacy summary",
            version=1,
        )
    )
    _record_turn(session, "new investigation " * 80, "new result " * 80)
    _record_turn(session, "latest", "latest answer")
    original_payload = legacy_checkpoint.payload_json
    engine = _RecordingCompactionEngine((_summary("upgraded"),))

    built = _build(
        ContextPipeline(engine),
        ContextInput(
            settings=ContextSettings(max_characters=1_500, max_summary_characters=900),
            active_branch=session.active_branch,
        ),
    )

    assert built.compaction is not None
    assert built.compaction.version == 2
    assert built.compaction.previous_checkpoint_id == "legacy-checkpoint"
    assert engine.inputs[0].previous_checkpoint is not None
    assert engine.inputs[0].previous_checkpoint.version == 1
    assert engine.inputs[0].previous_checkpoint.summary == "legacy summary"
    assert legacy_checkpoint.payload_json == original_payload


@pytest.mark.parametrize(
    ("summary", "previous_checkpoint_id", "error_pattern"),
    [
        (_summary("invalid"), "missing-checkpoint", "lineage"),
        ("unstructured stale summary", None, "summary"),
    ],
)
def test_replay_rejects_invalid_v2_checkpoint_contract(
    tmp_path: Path,
    summary: str,
    previous_checkpoint_id: str | None,
    error_pattern: str,
) -> None:
    path = tmp_path / "invalid-v2.jsonl"
    common = {
        "schema": "coding-agent-session",
        "version": 1,
        "session_id": "invalid-v2",
    }
    records = [
        {
            **common,
            "sequence": 1,
            "record_type": "entry",
            "entry": {
                "entry_id": "root",
                "parent_id": None,
                "kind": "configuration",
                "payload": {"provider": "fake"},
            },
        },
        {
            **common,
            "sequence": 2,
            "record_type": "entry",
            "entry": {
                "entry_id": "message",
                "parent_id": "root",
                "kind": "message",
                "payload": {"role": "user", "text": "preserve"},
            },
        },
        {
            **common,
            "sequence": 3,
            "record_type": "entry",
            "entry": {
                "entry_id": "checkpoint",
                "parent_id": "message",
                "kind": "compaction",
                "payload": {
                    "version": 2,
                    "summary": summary,
                    "covered_entry_ids": ["message"],
                    "first_kept_entry_id": None,
                    "previous_checkpoint_id": previous_checkpoint_id,
                    "evidence": {
                        "read_files": [],
                        "modified_files": [],
                        "commands": [],
                        "tool_errors": [],
                    },
                    "strategy": {"name": "semantic-checkpoint", "revision": "2"},
                    "metrics": {
                        "characters_before": 100,
                        "characters_after": 50,
                        "covered_count": 1,
                        "retained_count": 0,
                        "compaction_depth": 1,
                        "summary_usage": None,
                        "trigger": "automatic",
                        "thrashing_detected": False,
                    },
                },
            },
        },
        {**common, "sequence": 4, "record_type": "closed"},
    ]
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(SessionRelationError, match=error_pattern):
        Session.resume(JsonlSessionStore(path), "invalid-v2")


def test_summary_provider_failure_is_observable_atomic_and_precedes_coding_turn() -> None:
    session = _session_with_ids("root", "old-user", "old-answer")
    _record_turn(session, "Django regression " * 100, "diagnosis " * 100)
    original_ids = tuple(entry.entry_id for entry in session.active_branch)
    session.drain_events()
    provider = FakeProvider(
        (
            (ProviderError("provider_timeout", "summary timeout"),),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=900, max_summary_characters=500),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("continue the Django fix")
        events = [event async for event in run]
        return events, await run.result()

    events, result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "compaction_provider_failed"
    assert len(provider.requests) == 1
    assert provider.requests[0].tools == ()
    assert tuple(entry.entry_id for entry in session.active_branch) == original_ids
    assert all(entry.kind != "compaction" for entry in session.active_branch)
    kinds = [event.kind for event in events]
    assert AgentSessionEventKind.COMPACTION_STARTED in kinds
    assert AgentSessionEventKind.COMPACTION_FAILED in kinds
    assert AgentSessionEventKind.MESSAGE_UPDATE not in kinds


def test_summary_provider_self_cancellation_is_an_observable_compaction_failure() -> None:
    session = _session_with_ids("root", "old-user", "old-answer")
    _record_turn(session, "Django regression " * 100, "diagnosis " * 100)
    original_ids = tuple(entry.entry_id for entry in session.active_branch)
    session.drain_events()
    provider = _SelfCancellingSummaryProvider()
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=900, max_summary_characters=500),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("continue the Django fix")
        events = [event async for event in run]
        return events, await run.result()

    events, result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "compaction_provider_failed"
    assert "Provider cancelled its own stream task" in result.error.message
    assert len(provider.requests) == 1
    assert tuple(entry.entry_id for entry in session.active_branch) == original_ids
    kinds = [event.kind for event in events]
    assert AgentSessionEventKind.COMPACTION_STARTED in kinds
    assert AgentSessionEventKind.COMPACTION_FAILED in kinds


def test_summary_provider_stream_is_validated_and_closed_on_failure() -> None:
    session = _session_with_ids("root", "old-user", "old-answer")
    _record_turn(session, "Django regression " * 100, "diagnosis " * 100)
    provider = _ClosableSummaryProvider((object(),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=900, max_summary_characters=500),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue the Django fix")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "compaction_provider_failed"
    assert "Provider must emit a ProviderStreamEvent" in result.error.message
    assert provider.streams[0].closed is True


def test_extension_cannot_downgrade_a_new_v2_checkpoint_to_legacy_v1() -> None:
    session = _session_with_ids("root", "old-user", "old-answer", "checkpoint", "current", "answer")
    _record_turn(session, "Django regression " * 100, "diagnosis " * 100)
    provider = FakeProvider(
        (
            (ProviderTextDelta(_summary("semantic")), ProviderDone()),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=900, max_summary_characters=500),
        extensions=(_DowngradeCompactionExtension(),),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue the Django fix")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "extension_compaction_rejected"
    assert all(entry.kind != "compaction" for entry in session.active_branch)


def test_extension_cannot_persist_an_unstructured_v2_summary() -> None:
    session = _session_with_ids("root", "old-user", "old-answer", "checkpoint", "current")
    _record_turn(session, "Django regression " * 100, "diagnosis " * 100)
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_pipeline=ContextPipeline(_RecordingCompactionEngine((_summary("semantic"),))),
        context_settings=ContextSettings(max_characters=900, max_summary_characters=500),
        extensions=(_InvalidCompactionSummaryExtension(),),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue the Django fix")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "extension_compaction_rejected"
    assert all(entry.kind != "compaction" for entry in session.active_branch)
    assert provider.requests == []


def test_extension_changed_coverage_is_reprojected_from_the_final_plan() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-answer",
        "recent-user",
        "recent-answer",
        "checkpoint",
        "current",
        "answer",
    )
    _record_turn(session, "old question " * 100, "old answer " * 100)
    _record_turn(session, "recent raw user", "recent raw answer")
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_pipeline=ContextPipeline(_RecordingCompactionEngine((_summary("initial"),))),
        context_settings=ContextSettings(max_characters=1_200, max_summary_characters=500),
        extensions=(_ExpandCompactionCoverageExtension(),),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue the Django fix")
        async for _ in run:
            pass
        return await run.result()

    assert asyncio.run(collect()).state is AgentRunState.SETTLED
    checkpoint = next(entry for entry in session.active_branch if entry.kind == "compaction")
    assert checkpoint.payload["covered_entry_ids"] == [
        "old-user",
        "old-answer",
        "recent-user",
        "recent-answer",
    ]
    assert checkpoint.payload["first_kept_entry_id"] is None
    assert provider.requests[0].messages == (
        BranchSummaryMessage(text=_summary("extension-expanded")),
        UserMessage(text="continue the Django fix"),
    )


def test_persisted_characters_after_matches_final_supplemented_provider_request() -> None:
    session = _session_with_ids("root", "old-user", "old-answer", "checkpoint", "current", "answer")
    _record_turn(session, "Django regression " * 100, "diagnosis " * 100)
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_pipeline=ContextPipeline(_RecordingCompactionEngine((_summary("semantic"),))),
        context_settings=ContextSettings(max_characters=1_200, max_summary_characters=500),
        extensions=(_CompactedContextSupplementExtension(),),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue the Django fix")
        async for _ in run:
            pass
        return await run.result()

    assert asyncio.run(collect()).state is AgentRunState.SETTLED
    checkpoint = next(entry for entry in session.active_branch if entry.kind == "compaction")
    metrics = checkpoint.payload["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["final_characters"] == estimate_provider_request_characters(provider.requests[0])
    assert metrics["characters_after"] < metrics["final_characters"]
    assert provider.requests[0].messages[-1] == UserMessage(text="CURRENT_EXTENSION_ANNOTATION")


@pytest.mark.parametrize(
    ("extension", "expected_code"),
    [
        (_BlockProviderRequestExtension(), "extension_provider_blocked"),
        (_RewriteCheckpointSummaryExtension(), "extension_context_rejected"),
        (_LateProviderResourceExtension(), "extension_provider_rejected"),
        (
            _AppendCompactionMessageExtension(BranchSummaryMessage(text="duplicate summary")),
            "extension_context_rejected",
        ),
        (
            _AppendCompactionMessageExtension(UserMessage(text="reintroduced covered raw")),
            "extension_context_rejected",
        ),
    ],
)
def test_extension_failure_before_provider_dispatch_does_not_persist_checkpoint(
    extension: object,
    expected_code: str,
) -> None:
    session = _session_with_ids("root", "old-user", "old-answer", "checkpoint", "current", "answer")
    _record_turn(session, "reintroduced covered raw", "diagnosis " * 200)
    provider = FakeProvider(
        (
            (ProviderTextDelta(_summary("semantic")), ProviderDone()),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=1_600, max_summary_characters=500),
        extensions=(extension,),  # type: ignore[arg-type]
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunResult]:
        run = kernel.create_run("continue the Django fix")
        events = [event async for event in run]
        return events, await run.result()

    events, result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == expected_code
    assert len(provider.requests) == 1
    assert all(entry.kind != "compaction" for entry in session.active_branch)
    kinds = [event.kind for event in events]
    assert AgentSessionEventKind.COMPACTION_STARTED in kinds
    assert AgentSessionEventKind.COMPACTION_FAILED in kinds
    assert AgentSessionEventKind.CONTEXT_FAILED not in kinds


def test_session_revalidation_rejects_extension_cut_inside_tool_transaction() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-call",
        "old-result",
        "old-answer",
        "checkpoint",
        "current",
        "answer",
    )
    session.record_user_message("reproduce the failure " * 80)
    session.record_authoritative_message(
        AssistantMessage(tool_calls=(ToolCall("call-old", "shell", {"command": "pytest old"}),))
    )
    session.record_tool_result(
        ToolResult(
            "call-old",
            "shell",
            "error",
            {"exit_code": 1, "stdout": "failure output"},
            ToolError("tests_failed", "focused failure"),
        )
    )
    session.record_authoritative_message(AssistantMessage(text="inspect the assertion next"))
    provider = FakeProvider(
        (
            (ProviderTextDelta(_summary("semantic")), ProviderDone()),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=900, max_summary_characters=500),
        extensions=(_SplitToolTransactionExtension(),),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue the Django fix")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())

    assert result.state is AgentRunState.FAILED
    assert result.error is not None
    assert result.error.code == "extension_compaction_rejected"
    assert all(entry.kind != "compaction" for entry in session.active_branch)


def test_django_shaped_provider_compaction_persists_semantic_state_before_continuing() -> None:
    session = _session_with_ids(
        "root",
        "problem",
        "diagnosis",
        "command",
        "result",
        "checkpoint",
        "current",
        "answer",
    )
    session.record_user_message(
        "Fix a Django queryset regression while preserving existing behavior. " * 60
    )
    session.record_authoritative_message(
        AssistantMessage(
            text="The failing path is django/db/models/query.py QuerySet._chain.",
            tool_calls=(ToolCall("pytest", "shell", {"command": "pytest django/tests/queries"}),),
        )
    )
    session.record_tool_result(
        ToolResult(
            "pytest",
            "shell",
            "error",
            {
                "command": "pytest django/tests/queries",
                "cwd": "/testbed",
                "exit_code": 1,
                "modified_files": ["django/db/models/query.py"],
                "stdout": "REGENERABLE_TRACE " * 500,
            },
            ToolError("tests_failed", "one assertion still fails"),
        )
    )
    session.record_authoritative_message(
        AssistantMessage(text="Next inspect the clone behavior before changing _chain.")
    )
    semantic_summary = "\n".join(
        (
            "## Goal\nFix the Django queryset regression.",
            "## Constraints and preferences\nPreserve existing queryset behavior.",
            "## Done and verified\nLocated QuerySet._chain and ran the focused query tests.",
            "## In progress\nInspecting clone behavior.",
            "## Blocked\nOne focused assertion still fails.",
            "## Key decisions and relevant rejected approaches\nDo not rewrite unrelated ORM code.",
            "## Exact files, symbols, commands and errors\n"
            "django/db/models/query.py; QuerySet._chain; pytest django/tests/queries; exit 1.",
            "## Next steps\nInspect clone behavior, patch _chain, rerun focused tests.",
        )
    )
    provider = FakeProvider(
        (
            (
                ProviderTextDelta(semantic_summary),
                ProviderUsage(211, 83),
                ProviderDone(),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=1_400, max_summary_characters=900),
    )

    async def collect() -> AgentRunResult:
        run = kernel.create_run("continue from the current diagnosis")
        async for _ in run:
            pass
        return await run.result()

    result = asyncio.run(collect())

    assert result.state is AgentRunState.SETTLED
    assert len(provider.requests) == 2
    assert provider.requests[0].tools == ()
    assert "django/db/models/query.py" in getattr(provider.requests[0].messages[0], "text", "")
    summary_prompt = getattr(provider.requests[0].messages[0], "text", "")
    assert 0 < summary_prompt.count("REGENERABLE_TRACE") < 20
    assert [message.role for message in provider.requests[1].messages] == ["summary", "user"]
    assert provider.requests[1].resources[0].resource_id == "permission-mode"
    assert provider.requests[1].resources[0].revision == "auto"
    checkpoint = next(entry for entry in session.active_branch if entry.kind == "compaction")
    assert checkpoint.payload["version"] == 2
    assert checkpoint.payload["summary"] == semantic_summary
    metrics = checkpoint.payload["metrics"]
    evidence = checkpoint.payload["evidence"]
    assert isinstance(metrics, dict)
    assert isinstance(evidence, dict)
    assert metrics["summary_usage"] == {
        "input_tokens": 211,
        "output_tokens": 83,
    }
    assert evidence["modified_files"] == ["django/db/models/query.py"]


def test_extension_skill_resource_is_reprojected_and_never_enters_compaction_span() -> None:
    session = _session_with_ids(
        "root",
        "old-user",
        "old-answer",
        "checkpoint",
        "first-user",
        "first-answer",
        "second-user",
        "second-answer",
    )
    _record_turn(session, "old context " * 120, "old result " * 120)
    engine = _RecordingCompactionEngine((_summary("extension"),))
    extension = _SkillResourceExtension()
    provider = FakeProvider(((ProviderDone(),), (ProviderDone(),)))
    kernel = AgentKernel(
        provider,
        session=session,
        context_pipeline=ContextPipeline(engine),
        context_settings=ContextSettings(max_characters=1_800, max_summary_characters=900),
        extensions=(extension,),
    )

    async def run_once(prompt: str) -> AgentRunResult:
        run = kernel.create_run(prompt)
        async for _ in run:
            pass
        return await run.result()

    assert asyncio.run(run_once("continue first")).state is AgentRunState.SETTLED
    compaction_resources = {
        resource.resource_id: resource for resource in engine.inputs[0].authoritative_resources
    }
    assert compaction_resources["skill:active"].revision == "sha256:first"
    assert "FIRST_SKILL_RULE" not in repr(engine.inputs[0].newly_covered_entries)
    first_skill = next(
        resource
        for resource in provider.requests[0].resources
        if resource.resource_id == "skill:active"
    )
    assert first_skill.revision == "sha256:first"

    extension.revision = "sha256:second"
    extension.content = "SECOND_SKILL_RULE"
    assert asyncio.run(run_once("continue second")).state is AgentRunState.SETTLED
    second_skill = next(
        resource
        for resource in provider.requests[1].resources
        if resource.resource_id == "skill:active"
    )
    assert second_skill.revision == "sha256:second"
    assert second_skill.content == "SECOND_SKILL_RULE"
    assert "FIRST_SKILL_RULE" not in repr(provider.requests[1])
