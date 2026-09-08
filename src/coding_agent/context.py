"""The canonical Model Context construction pipeline."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Final, Literal, Protocol, cast

from coding_agent.callout import dispose_awaitable
from coding_agent.compaction import (
    COMPACTION_SUMMARY_HEADINGS,
    CompactionCheckpoint,
    CompactionCommandEvidence,
    CompactionContractError,
    CompactionEvidence,
    CompactionMetrics,
    CompactionPlan,
    CompactionStrategy,
    CompactionToolErrorEvidence,
    decode_v2_compaction_checkpoint,
    validate_compaction_summary,
)
from coding_agent.events import (
    AssistantMessage,
    ProviderAbort,
    ProviderCancelled,
    ProviderDone,
    ProviderError,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ProviderUsage,
    TokenUsage,
    ToolCall,
    ToolError,
    ToolResult,
    assistant_message_record,
    tool_result_record,
    validate_provider_stream_event,
)
from coding_agent.provider import (
    BranchSummaryMessage,
    ContextResource,
    ModelMessage,
    ModelProvider,
    ProviderRequest,
    ToolResultMessage,
    UserMessage,
    isolated_provider_close,
    isolated_provider_events,
    isolated_provider_factory,
)
from coding_agent.session import SessionEntry

ASSEMBLY_ORDER: Final = (
    "system_prompt",
    "active_tools",
    "authoritative_resources",
    "project_context",
    "active_branch",
    "injected_messages",
    "provider_request",
)


@dataclass(frozen=True, slots=True)
class ContextSettings:
    """Stable inputs that apply to every Model Context construction."""

    system_prompt: str = "You are a headless coding agent."
    tool_guidelines: str = "Use only the active tools described in this request."
    project_context: tuple[str, ...] = ()
    authoritative_resources: tuple[ContextResource, ...] = ()
    max_characters: int = 100_000
    max_summary_characters: int = 12_000

    def __post_init__(self) -> None:
        if self.max_characters <= 0:
            raise ValueError("max_characters must be positive")
        if self.max_summary_characters <= 0:
            raise ValueError("max_summary_characters must be positive")


@dataclass(frozen=True, slots=True)
class ContextInput:
    """A pure snapshot; it never exposes mutable Session or Agent Run state."""

    settings: ContextSettings
    active_branch: tuple[SessionEntry, ...] = ()
    active_tools: tuple[dict[str, object], ...] = ()
    authoritative_resources: tuple[ContextResource, ...] = ()
    injected_messages: tuple[ModelMessage, ...] = ()
    pending_messages: tuple[ModelMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactionInput:
    previous_checkpoint: CompactionCheckpoint | None
    newly_covered_entries: tuple[SessionEntry, ...]
    retained_recent_entries: tuple[SessionEntry, ...]
    authoritative_resources: tuple[ContextResource, ...]
    max_summary_characters: int
    focus: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionDraft:
    summary: str
    usage: TokenUsage | None = None


class CompactionEngine(Protocol):
    def compact(self, compaction_input: CompactionInput) -> Awaitable[CompactionDraft]: ...


@dataclass(frozen=True, slots=True)
class ModelContext:
    """An immutable, inspectable value for exactly one Provider request."""

    provider_request: ProviderRequest
    estimated_characters: int
    max_characters: int
    assembly_order: tuple[str, ...] = ASSEMBLY_ORDER

    @property
    def bounded(self) -> bool:
        return self.estimated_characters <= self.max_characters


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    context: ModelContext
    compaction: CompactionPlan | None = None
    covered_messages: tuple[ModelMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextHookInput:
    """Immutable input for the production Context Extension Hook."""

    context: ModelContext


@dataclass(frozen=True, slots=True)
class ContextHookOutput:
    """Typed Context value retained for callers that wrap a transformed Context."""

    context: ModelContext


class BranchSummarizer(Protocol):
    def summarize(self, messages: tuple[ModelMessage, ...]) -> str: ...


class ContextConstructionError(ValueError):
    """An explicit pre-Provider failure in the canonical Context pipeline."""

    def __init__(self, code: str, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


@dataclass(frozen=True, slots=True)
class DeterministicBranchSummarizer:
    """A local character-bounded summarizer, not a tokenizer or model call."""

    max_summary_characters: int = 180

    def summarize(self, messages: tuple[ModelMessage, ...]) -> str:
        lines: list[str] = []
        for message in messages:
            if isinstance(message, ToolResultMessage):
                text = ",".join(result.call_id for result in message.results)
            else:
                text = message.text
            lines.append(f"{message.role}:{text}")
        joined = "\n".join(lines)
        if len(joined) <= self.max_summary_characters:
            return joined
        return joined[: self.max_summary_characters - 1] + "…"


_COMPACTION_SYSTEM_PROMPT: Final = """You create a semantic checkpoint for a coding agent.
Return concise Markdown with exactly these level-2 headings, in this order:
Goal; Constraints and preferences; Done and verified; In progress; Blocked;
Key decisions and relevant rejected approaches; Exact files, symbols, commands and errors;
Next steps. Update or remove superseded facts. Do not copy role prefixes or create nested
summary wrappers. Treat the supplied authoritative-resource metadata as current context, not
conversation facts. Do not call tools."""


def _structured_summary(content: str) -> str:
    sections = []
    for index, heading in enumerate(COMPACTION_SUMMARY_HEADINGS):
        value = content[:12] if index == 0 else "-"
        sections.append(f"## {heading}\n{value}")
    return "\n".join(sections)


@dataclass(frozen=True, slots=True)
class DeterministicCompactionEngine:
    """Deterministic development engine; production Kernels use their ModelProvider."""

    summarizer: BranchSummarizer = field(default_factory=DeterministicBranchSummarizer)

    async def compact(self, compaction_input: CompactionInput) -> CompactionDraft:
        messages = tuple(
            message
            for entry in compaction_input.newly_covered_entries
            if (message := _message_from_entry(entry)) is not None
        )
        content = self.summarizer.summarize(messages)
        return CompactionDraft(_structured_summary(content))


@dataclass(frozen=True, slots=True)
class ProviderCompactionEngine:
    """Semantic compaction through the existing provider-neutral ModelProvider seam."""

    provider: ModelProvider

    async def compact(self, compaction_input: CompactionInput) -> CompactionDraft:
        prompt = _compaction_prompt(compaction_input)
        request = ProviderRequest(
            messages=(UserMessage(text=prompt),),
            tools=(),
            system_prompt=_COMPACTION_SYSTEM_PROMPT,
        )
        text_parts: list[str] = []
        usage: TokenUsage | None = None
        done = False
        try:
            stream_candidate = await isolated_provider_factory(self.provider, request)
            if inspect.isawaitable(stream_candidate):
                dispose_awaitable(stream_candidate)
                raise TypeError("Provider.stream must return an async iterator")
            if not isinstance(stream_candidate, AsyncIterator):
                raise TypeError("Provider.stream must return an async iterator")
            stream = stream_candidate
            close_stream = getattr(stream, "aclose", None)
            primary_error: BaseException | None = None
            try:
                async for raw_event in isolated_provider_events(stream):
                    event = validate_provider_stream_event(raw_event)
                    if isinstance(event, ProviderTextDelta):
                        text_parts.append(event.delta)
                    elif isinstance(event, ProviderUsage):
                        usage = TokenUsage(event.input_tokens, event.output_tokens)
                    elif isinstance(event, ProviderDone):
                        done = True
                    elif isinstance(event, ProviderError):
                        raise ContextConstructionError(
                            "compaction_provider_failed",
                            f"Compaction Provider failed ({event.code}): {event.message}",
                            stage="compaction",
                        )
                    elif isinstance(event, (ProviderAbort, ProviderCancelled)):
                        raise ContextConstructionError(
                            "compaction_provider_failed",
                            f"Compaction Provider stopped: {event.reason}",
                            stage="compaction",
                        )
                    elif isinstance(
                        event,
                        (ProviderToolCallStart, ProviderToolCallDelta, ProviderToolCallEnd),
                    ):
                        raise ContextConstructionError(
                            "compaction_summary_invalid",
                            "Compaction Provider attempted to call a Tool.",
                            stage="compaction",
                        )
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if callable(close_stream):
                    try:
                        await isolated_provider_close(close_stream)
                    except BaseException:
                        if primary_error is None:
                            raise
        except ContextConstructionError:
            raise
        except Exception as exc:
            raise ContextConstructionError(
                "compaction_provider_failed",
                f"Compaction Provider raised {type(exc).__name__}: {exc}",
                stage="compaction",
            ) from exc
        if not done:
            raise ContextConstructionError(
                "compaction_provider_failed",
                "Compaction Provider stream ended without done.",
                stage="compaction",
            )
        return CompactionDraft("".join(text_parts).strip(), usage)


def _compaction_prompt(compaction_input: CompactionInput) -> str:
    previous = compaction_input.previous_checkpoint
    previous_record: dict[str, object] | None = None
    if previous is not None:
        previous_record = {
            "entry_id": previous.entry_id,
            "version": previous.version,
            "summary": previous.summary,
            "evidence": previous.evidence.record(),
        }
    entries: list[dict[str, object]] = []
    tool_calls: dict[str, ToolCall] = {}
    for entry in compaction_input.newly_covered_entries:
        message = _message_from_entry(entry)
        if message is None:
            continue
        if isinstance(message, AssistantMessage):
            tool_calls.update((call.call_id, call) for call in message.tool_calls)
        elif isinstance(message, ToolResultMessage):
            message = ToolResultMessage(
                results=tuple(
                    _shed_tool_result(result, tool_calls.get(result.call_id))
                    for result in message.results
                )
            )
        entries.append({"entry_id": entry.entry_id, "message": _model_message_record(message)})
    resources = [
        {
            "source": resource.source,
            "authority": resource.authority,
            "resource_id": resource.resource_id,
            "revision": resource.revision,
        }
        for resource in compaction_input.authoritative_resources
    ]
    value = {
        "previous_checkpoint": previous_record,
        "new_conversation_span": entries,
        "authoritative_resource_metadata": resources,
        "focus": compaction_input.focus,
        "max_summary_characters": compaction_input.max_summary_characters,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _message_from_entry(entry: SessionEntry) -> ModelMessage | None:
    if entry.kind != "message":
        return None
    payload = entry.payload
    role = payload.get("role")
    if role == "user":
        return UserMessage(text=str(payload.get("text", "")))
    if role == "assistant":
        raw_calls_value = payload.get("tool_calls", [])
        raw_calls = raw_calls_value if isinstance(raw_calls_value, list) else []
        tool_calls: list[ToolCall] = []
        for call in raw_calls:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("call_id"), str)
                or not isinstance(call.get("tool_name"), str)
                or not isinstance(call.get("arguments"), dict)
            ):
                raise ContextConstructionError(
                    "context_entry_invalid",
                    f"Assistant SessionEntry {entry.entry_id!r} has an invalid ToolCall.",
                    stage="context",
                )
            tool_calls.append(
                ToolCall(
                    call_id=call["call_id"],
                    tool_name=call["tool_name"],
                    arguments=call["arguments"],
                )
            )
        raw_usage = payload.get("usage")
        usage = None
        if raw_usage is not None:
            if (
                not isinstance(raw_usage, dict)
                or type(raw_usage.get("input_tokens")) is not int
                or type(raw_usage.get("output_tokens")) is not int
            ):
                raise ContextConstructionError(
                    "context_entry_invalid",
                    f"Assistant SessionEntry {entry.entry_id!r} has invalid usage.",
                    stage="context",
                )
            usage = TokenUsage(
                input_tokens=raw_usage["input_tokens"],
                output_tokens=raw_usage["output_tokens"],
            )
        return AssistantMessage(
            text=str(payload.get("text", "")),
            thinking=str(payload.get("thinking", "")),
            tool_calls=tuple(tool_calls),
            usage=usage,
            stop_reason=(
                None if payload.get("stop_reason") is None else str(payload["stop_reason"])
            ),
            response_id=(
                None if payload.get("response_id") is None else str(payload["response_id"])
            ),
        )
    if role == "tool":
        raw_results = payload.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            raise ContextConstructionError(
                "context_entry_invalid",
                f"Tool SessionEntry {entry.entry_id!r} must contain results.",
                stage="context",
            )
        results: list[ToolResult] = []
        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                raise ContextConstructionError(
                    "context_entry_invalid",
                    f"Tool SessionEntry {entry.entry_id!r} has an invalid result.",
                    stage="context",
                )
            call_id = raw_result.get("call_id")
            tool_name = raw_result.get("tool_name")
            raw_status = raw_result.get("status")
            output = raw_result.get("output")
            raw_error = raw_result.get("error")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(tool_name, str)
                or not tool_name
                or raw_status not in {"success", "error", "cancelled"}
                or (output is not None and not isinstance(output, dict))
            ):
                raise ContextConstructionError(
                    "context_entry_invalid",
                    f"Tool SessionEntry {entry.entry_id!r} has invalid result fields.",
                    stage="context",
                )
            error = None
            if raw_error is not None:
                if (
                    not isinstance(raw_error, dict)
                    or not isinstance(raw_error.get("code"), str)
                    or not isinstance(raw_error.get("message"), str)
                ):
                    raise ContextConstructionError(
                        "context_entry_invalid",
                        f"Tool SessionEntry {entry.entry_id!r} has an invalid error.",
                        stage="context",
                    )
                error = ToolError(raw_error["code"], raw_error["message"])
            results.append(
                ToolResult(
                    call_id,
                    tool_name,
                    cast(Literal["success", "error", "cancelled"], raw_status),
                    output,
                    error,
                )
            )
        return ToolResultMessage(results=tuple(results))
    raise ContextConstructionError(
        "context_entry_invalid",
        f"Message SessionEntry {entry.entry_id!r} has unsupported role {role!r}.",
        stage="context",
    )


def _model_message_record(message: ModelMessage) -> dict[str, object]:
    if isinstance(message, ToolResultMessage):
        return {
            "role": message.role,
            "results": [tool_result_record(result) for result in message.results],
        }
    if isinstance(message, AssistantMessage):
        return assistant_message_record(message)
    return {"role": message.role, "text": message.text}


def _request_record(request: ProviderRequest) -> dict[str, object]:

    return {
        "system_prompt": request.system_prompt,
        "tools": list(request.tools),
        "tool_guidelines": request.tool_guidelines,
        "project_context": list(request.project_context),
        "messages": [_model_message_record(message) for message in request.messages],
        "resources": [
            {
                "source": resource.source,
                "authority": resource.authority,
                "resource_id": resource.resource_id,
                "revision": resource.revision,
                "content": resource.content,
            }
            for resource in request.resources
        ],
    }


def estimate_provider_request_characters(request: ProviderRequest) -> int:
    """Count canonical JSON characters; this is deliberately not a token count."""

    try:
        encoded = json.dumps(
            _request_record(request),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContextConstructionError(
            "context_input_invalid",
            f"Model Context is not JSON serializable: {type(exc).__name__}: {exc}",
            stage="context",
        ) from exc
    return len(encoded)


class ContextPipeline:
    """The one authoritative Context projection and compaction boundary."""

    def __init__(
        self,
        engine: CompactionEngine | BranchSummarizer | None = None,
    ) -> None:
        if engine is None:
            self._engine: CompactionEngine = DeterministicCompactionEngine()
        elif hasattr(engine, "compact"):
            self._engine = cast(CompactionEngine, engine)
        else:
            self._engine = DeterministicCompactionEngine(engine)

    async def build(
        self,
        context_input: ContextInput,
        *,
        on_compaction_start: Callable[[], None] | None = None,
    ) -> ContextBuildResult:
        _ = context_input.pending_messages  # pending Agent Run state is never model input
        branch_messages = self._project_active_branch(context_input.active_branch)
        request = self._request(context_input, branch_messages)
        characters_before = estimate_provider_request_characters(request)
        if characters_before <= context_input.settings.max_characters:
            return ContextBuildResult(
                ModelContext(
                    provider_request=request,
                    estimated_characters=characters_before,
                    max_characters=context_input.settings.max_characters,
                )
            )

        shed_branch_messages = self._project_active_branch(
            context_input.active_branch,
            shed_tool_outputs=True,
        )
        shed_request = self._request(context_input, shed_branch_messages)
        shed_characters = estimate_provider_request_characters(shed_request)
        if shed_characters <= context_input.settings.max_characters:
            return ContextBuildResult(
                ModelContext(
                    provider_request=shed_request,
                    estimated_characters=shed_characters,
                    max_characters=context_input.settings.max_characters,
                )
            )

        previous = self._latest_checkpoint(context_input.active_branch)
        conversation_entries = tuple(
            entry for entry in context_input.active_branch if entry.kind == "message"
        )
        previous_coverage = () if previous is None else previous.covered_entry_ids
        uncovered = tuple(
            entry for entry in conversation_entries if entry.entry_id not in previous_coverage
        )
        if not uncovered:
            raise ContextConstructionError(
                "context_budget_exceeded" if previous is None else "compaction_thrashing",
                (
                    "Model Context exceeds its character budget and has no branch history "
                    "to compact."
                    if previous is None
                    else "Model Context remains over budget but no new conversation span can "
                    "be compacted."
                ),
                stage="context" if previous is None else "compaction",
            )

        summary_budget = min(
            context_input.settings.max_summary_characters,
            max(256, context_input.settings.max_characters // 2),
        )
        turns = _conversation_turns(uncovered)
        retained = self._select_retained_turns(
            context_input,
            turns,
            summary_budget=summary_budget,
        )
        if len(retained) == len(uncovered):
            raise ContextConstructionError(
                "context_budget_exceeded",
                "Model Context exceeds its character budget and has no safe old turn to compact.",
                stage="context",
            )
        if on_compaction_start is not None:
            on_compaction_start()
        retained_turns = _conversation_turns(retained)
        summary_usage: TokenUsage | None = None
        while True:
            retained = tuple(entry for turn in retained_turns for entry in turn)
            retained_ids = {entry.entry_id for entry in retained}
            newly_covered = tuple(
                entry for entry in uncovered if entry.entry_id not in retained_ids
            )
            if not newly_covered:
                raise ContextConstructionError(
                    "context_budget_exceeded",
                    "Model Context exceeds its character budget and has no safe old turn "
                    "to compact.",
                    stage="context",
                )
            _validate_safe_cut(newly_covered, retained)
            compaction_input = CompactionInput(
                previous_checkpoint=previous,
                newly_covered_entries=newly_covered,
                retained_recent_entries=retained,
                authoritative_resources=(
                    *context_input.settings.authoritative_resources,
                    *context_input.authoritative_resources,
                ),
                max_summary_characters=summary_budget,
            )
            try:
                draft = await self._engine.compact(compaction_input)
            except ContextConstructionError:
                raise
            except Exception as exc:
                raise ContextConstructionError(
                    "compaction_summary_failed",
                    f"Compaction summary failed: {type(exc).__name__}: {exc}",
                    stage="compaction",
                ) from exc
            _validate_summary(draft.summary, summary_budget)
            summary_usage = _add_token_usage(summary_usage, draft.usage)

            compacted_request: ProviderRequest | None = None
            characters_after = 0
            for shed_tool_outputs in (False, True):
                retained_messages = _project_entries(
                    retained,
                    shed_tool_outputs=shed_tool_outputs,
                )
                candidate_request = self._request(
                    context_input,
                    (BranchSummaryMessage(text=draft.summary), *retained_messages),
                )
                candidate_characters = estimate_provider_request_characters(candidate_request)
                if candidate_characters <= context_input.settings.max_characters:
                    compacted_request = candidate_request
                    characters_after = candidate_characters
                    break
            if compacted_request is not None:
                break
            if not retained_turns:
                raise ContextConstructionError(
                    "context_budget_exceeded",
                    "Compacted Model Context still exceeds its character budget.",
                    stage="context",
                )
            retained_turns = retained_turns[1:]

        reduction = characters_before - characters_after
        thrashing_detected = previous is not None and reduction < max(32, characters_before // 20)

        coverage = (*previous_coverage, *(entry.entry_id for entry in newly_covered))
        evidence = _merge_evidence(
            CompactionEvidence() if previous is None else previous.evidence,
            _extract_evidence(newly_covered),
        )
        depth = 1 if previous is None else previous.metrics.compaction_depth + 1
        plan = CompactionPlan(
            covered_entry_ids=coverage,
            summary=draft.summary,
            first_kept_entry_id=(None if not retained else retained[0].entry_id),
            previous_checkpoint_id=(None if previous is None else previous.entry_id),
            evidence=evidence,
            strategy=CompactionStrategy(),
            metrics=CompactionMetrics(
                characters_before=characters_before,
                characters_after=characters_after,
                final_characters=characters_after,
                covered_count=len(coverage),
                retained_count=len(retained),
                compaction_depth=depth,
                summary_usage=summary_usage,
                thrashing_detected=thrashing_detected,
            ),
        )
        return ContextBuildResult(
            ModelContext(
                provider_request=compacted_request,
                estimated_characters=characters_after,
                max_characters=context_input.settings.max_characters,
            ),
            plan,
            _project_entries(
                tuple(entry for entry in context_input.active_branch if entry.entry_id in coverage)
            ),
        )

    def reproject_compaction(
        self,
        context_input: ContextInput,
        plan: CompactionPlan,
    ) -> ContextBuildResult:
        """Rebuild the canonical request from one Session-validated final plan."""

        conversation_entries = tuple(
            entry for entry in context_input.active_branch if entry.kind == "message"
        )
        covered_count = len(plan.covered_entry_ids)
        covered_entries = conversation_entries[:covered_count]
        retained_entries = conversation_entries[covered_count:]
        projected_request: ProviderRequest | None = None
        characters_after = 0
        for shed_tool_outputs in (False, True):
            retained_messages = _project_entries(
                retained_entries,
                shed_tool_outputs=shed_tool_outputs,
            )
            candidate_request = self._request(
                context_input,
                (BranchSummaryMessage(text=plan.summary), *retained_messages),
            )
            candidate_characters = estimate_provider_request_characters(candidate_request)
            if candidate_characters <= context_input.settings.max_characters:
                projected_request = candidate_request
                characters_after = candidate_characters
                break
        if projected_request is None:
            raise ContextConstructionError(
                "extension_compaction_rejected",
                "Transformed compaction plan exceeds the canonical Context budget.",
                stage="compaction",
            )

        metrics = replace(
            plan.metrics,
            characters_after=characters_after,
            final_characters=characters_after,
            covered_count=covered_count,
            retained_count=len(retained_entries),
            thrashing_detected=(
                plan.metrics.compaction_depth > 1
                and plan.metrics.characters_before - characters_after
                < max(32, plan.metrics.characters_before // 20)
            ),
        )
        reprojected_plan = replace(plan, metrics=metrics)
        return ContextBuildResult(
            ModelContext(
                provider_request=projected_request,
                estimated_characters=characters_after,
                max_characters=context_input.settings.max_characters,
            ),
            reprojected_plan,
            _project_entries(covered_entries),
        )

    @staticmethod
    def _request(
        context_input: ContextInput,
        branch_messages: tuple[ModelMessage, ...],
    ) -> ProviderRequest:
        return ProviderRequest(
            messages=branch_messages + context_input.injected_messages,
            tools=context_input.active_tools,
            system_prompt=context_input.settings.system_prompt,
            tool_guidelines=context_input.settings.tool_guidelines,
            project_context=context_input.settings.project_context,
            resources=(
                *context_input.settings.authoritative_resources,
                *context_input.authoritative_resources,
            ),
        )

    @staticmethod
    def _select_retained_turns(
        context_input: ContextInput,
        turns: tuple[tuple[SessionEntry, ...], ...],
        *,
        summary_budget: int,
    ) -> tuple[SessionEntry, ...]:
        if not turns:
            return ()
        summary_placeholder = BranchSummaryMessage(text="x" * summary_budget)
        selected: tuple[tuple[SessionEntry, ...], ...] = ()
        for turn in reversed(turns):
            candidate_turns = (turn, *selected)
            candidate_entries = tuple(entry for group in candidate_turns for entry in group)
            messages = _project_entries(candidate_entries)
            candidate_request = ContextPipeline._request(
                context_input,
                (summary_placeholder, *messages),
            )
            if estimate_provider_request_characters(candidate_request) <= (
                context_input.settings.max_characters
            ):
                selected = candidate_turns
                continue
            shed_messages = _project_entries(candidate_entries, shed_tool_outputs=True)
            shed_request = ContextPipeline._request(
                context_input,
                (summary_placeholder, *shed_messages),
            )
            if estimate_provider_request_characters(shed_request) <= (
                context_input.settings.max_characters
            ):
                selected = candidate_turns
                continue
            break
        return tuple(entry for turn in selected for entry in turn)

    @staticmethod
    def _project_active_branch(
        active_branch: tuple[SessionEntry, ...],
        *,
        shed_tool_outputs: bool = False,
    ) -> tuple[ModelMessage, ...]:
        checkpoint_index: int | None = None
        for index, entry in enumerate(active_branch):
            if entry.kind == "compaction":
                checkpoint_index = index

        projected: list[ModelMessage] = []
        start = 0
        if checkpoint_index is not None:
            checkpoint = ContextPipeline._checkpoint_at(active_branch, checkpoint_index)
            projected.append(BranchSummaryMessage(text=checkpoint.summary))
            if checkpoint.version == 1:
                start = checkpoint_index + 1
            elif checkpoint.first_kept_entry_id is None:
                start = checkpoint_index + 1
            else:
                start = next(
                    (
                        index
                        for index, entry in enumerate(active_branch)
                        if entry.entry_id == checkpoint.first_kept_entry_id
                    ),
                    -1,
                )
                if start < 0 or start >= checkpoint_index:
                    raise ContextConstructionError(
                        "compaction_checkpoint_invalid",
                        f"Compaction checkpoint {checkpoint.entry_id!r} has an invalid boundary.",
                        stage="context",
                    )

        projected.extend(
            _project_entries(
                tuple(
                    entry
                    for entry in active_branch[start:checkpoint_index]
                    if checkpoint_index is not None and entry.kind != "compaction"
                ),
                shed_tool_outputs=shed_tool_outputs,
            )
        )
        if checkpoint_index is None:
            projected.extend(_project_entries(active_branch, shed_tool_outputs=shed_tool_outputs))
        else:
            projected.extend(
                _project_entries(
                    tuple(
                        entry
                        for entry in active_branch[checkpoint_index + 1 :]
                        if entry.kind != "compaction"
                    ),
                    shed_tool_outputs=shed_tool_outputs,
                )
            )
        return tuple(projected)

    @staticmethod
    def _latest_checkpoint(
        active_branch: tuple[SessionEntry, ...],
    ) -> CompactionCheckpoint | None:
        for index in range(len(active_branch) - 1, -1, -1):
            if active_branch[index].kind == "compaction":
                return ContextPipeline._checkpoint_at(active_branch, index)
        return None

    @staticmethod
    def _checkpoint_at(
        active_branch: tuple[SessionEntry, ...],
        index: int,
    ) -> CompactionCheckpoint:
        entry = active_branch[index]
        payload = entry.payload
        version = payload.get("version")
        summary = payload.get("summary")
        raw_coverage = payload.get("covered_entry_ids")
        if (
            version not in {1, 2}
            or not isinstance(summary, str)
            or not summary
            or not isinstance(raw_coverage, list)
            or not all(isinstance(item, str) for item in raw_coverage)
        ):
            raise ContextConstructionError(
                "compaction_checkpoint_invalid",
                f"Compaction checkpoint {entry.entry_id!r} is invalid.",
                stage="context",
            )
        coverage = tuple(raw_coverage)
        if version == 1:
            expected = tuple(
                candidate.entry_id
                for candidate in active_branch[:index]
                if candidate.kind != "configuration"
            )
            if coverage != expected:
                raise ContextConstructionError(
                    "compaction_checkpoint_invalid",
                    f"Compaction checkpoint {entry.entry_id!r} is invalid.",
                    stage="context",
                )
            return CompactionCheckpoint(
                entry.entry_id,
                1,
                summary,
                tuple(
                    candidate.entry_id
                    for candidate in active_branch[:index]
                    if candidate.kind == "message"
                ),
                None,
                None,
                CompactionEvidence(),
                CompactionStrategy("legacy-prefix", "1"),
                CompactionMetrics(
                    covered_count=sum(
                        candidate.kind == "message" for candidate in active_branch[:index]
                    ),
                    retained_count=0,
                    compaction_depth=1,
                ),
            )
        message_ids = tuple(
            candidate.entry_id for candidate in active_branch[:index] if candidate.kind == "message"
        )
        previous_index = next(
            (
                candidate_index
                for candidate_index in range(index - 1, -1, -1)
                if active_branch[candidate_index].kind == "compaction"
            ),
            None,
        )
        previous_checkpoint = (
            None
            if previous_index is None
            else ContextPipeline._checkpoint_at(active_branch, previous_index)
        )
        try:
            return decode_v2_compaction_checkpoint(
                entry.entry_id,
                payload,
                message_ids=message_ids,
                previous_checkpoint_id=(
                    None if previous_checkpoint is None else previous_checkpoint.entry_id
                ),
                previous_coverage=(
                    () if previous_checkpoint is None else previous_checkpoint.covered_entry_ids
                ),
                expected_depth=(
                    1
                    if previous_checkpoint is None
                    else previous_checkpoint.metrics.compaction_depth + 1
                ),
            )
        except CompactionContractError as exc:
            raise ContextConstructionError(
                "compaction_checkpoint_invalid",
                f"Compaction checkpoint {entry.entry_id!r} {exc}.",
                stage="context",
            ) from exc


def _conversation_turns(
    entries: tuple[SessionEntry, ...],
) -> tuple[tuple[SessionEntry, ...], ...]:
    turns: list[list[SessionEntry]] = []
    for entry in entries:
        message = _message_from_entry(entry)
        if isinstance(message, UserMessage) or not turns:
            turns.append([entry])
        else:
            turns[-1].append(entry)
    return tuple(tuple(turn) for turn in turns)


def _validate_safe_cut(
    covered: tuple[SessionEntry, ...],
    retained: tuple[SessionEntry, ...],
) -> None:
    covered_calls, covered_results = _tool_transaction_ids(covered)
    retained_calls, retained_results = _tool_transaction_ids(retained)
    if covered_calls.intersection(retained_results) or retained_calls.intersection(covered_results):
        raise ContextConstructionError(
            "compaction_safe_cut_unavailable",
            "Compaction cannot split an Assistant ToolCall from its ToolResult.",
            stage="compaction",
        )


def _tool_transaction_ids(entries: tuple[SessionEntry, ...]) -> tuple[set[str], set[str]]:
    calls: set[str] = set()
    results: set[str] = set()
    for entry in entries:
        message = _message_from_entry(entry)
        if isinstance(message, AssistantMessage):
            calls.update(call.call_id for call in message.tool_calls)
        elif isinstance(message, ToolResultMessage):
            results.update(result.call_id for result in message.results)
    return calls, results


def _project_entries(
    entries: tuple[SessionEntry, ...],
    *,
    shed_tool_outputs: bool = False,
) -> tuple[ModelMessage, ...]:
    messages: list[ModelMessage] = []
    tool_calls: dict[str, ToolCall] = {}
    for entry in entries:
        message = _message_from_entry(entry)
        if message is None:
            continue
        if isinstance(message, AssistantMessage):
            tool_calls.update((call.call_id, call) for call in message.tool_calls)
        if shed_tool_outputs and isinstance(message, ToolResultMessage):
            message = ToolResultMessage(
                results=tuple(
                    _shed_tool_result(result, tool_calls.get(result.call_id))
                    for result in message.results
                )
            )
        messages.append(message)
    return tuple(messages)


def _shed_tool_result(result: ToolResult, call: ToolCall | None) -> ToolResult:
    output = result.output
    if output is None:
        return result
    tool_name = result.tool_name if call is None else call.tool_name
    if tool_name not in {"bash", "shell", "read", "grep", "find", "ls"}:
        return result
    encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= 512:
        return result
    preserved: dict[str, object] = {
        "compaction_shed": True,
        "original_characters": len(encoded),
    }
    for key in (
        "command",
        "cwd",
        "exit_code",
        "exit_status",
        "path",
        "file",
        "status",
        "error_code",
    ):
        value = output.get(key)
        if isinstance(value, (str, int, bool)):
            preserved[key] = value
    for key in ("stdout", "stderr"):
        value = output.get(key)
        if isinstance(value, str) and value:
            preserved[f"{key}_excerpt"] = _bounded_output_excerpt(value)
    content = output.get("content")
    if tool_name == "read" and isinstance(content, str) and content:
        preserved["content_excerpt"] = _bounded_output_excerpt(content)
    collection_key = {"grep": "matches", "find": "paths", "ls": "entries"}.get(tool_name)
    if collection_key is not None:
        collection = output.get(collection_key)
        if isinstance(collection, list):
            preserved[f"{collection_key}_count"] = len(collection)
            preserved[f"{collection_key}_excerpt"] = _bounded_collection(collection)
    if call is not None:
        for key in ("command", "cwd", "path"):
            value = call.arguments.get(key)
            if isinstance(value, str):
                preserved.setdefault(key, value)
    return ToolResult(
        result.call_id,
        result.tool_name,
        result.status,
        preserved,
        result.error,
    )


def _bounded_output_excerpt(value: str, *, max_characters: int = 160) -> str:
    if len(value) <= max_characters:
        return value
    half = (max_characters - len("…<shed>…")) // 2
    return f"{value[:half]}…<shed>…{value[-half:]}"


def _bounded_collection(value: list[object], *, max_items: int = 4) -> list[object]:
    selected = value
    if len(value) > max_items:
        half = max_items // 2
        selected = [*value[:half], *value[-half:]]
    bounded: list[object] = []
    for item in selected:
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        bounded.append(
            item if len(encoded) <= 120 else {"json_excerpt": _bounded_output_excerpt(encoded)}
        )
    return bounded


def _validate_summary(summary: str, max_characters: int) -> None:
    try:
        validate_compaction_summary(summary, max_characters=max_characters)
    except CompactionContractError as exc:
        raise ContextConstructionError(
            "compaction_summary_invalid",
            f"Compaction summary {exc}.",
            stage="compaction",
        ) from exc


def _add_token_usage(
    accumulated: TokenUsage | None,
    current: TokenUsage | None,
) -> TokenUsage | None:
    if current is None:
        return accumulated
    if accumulated is None:
        return current
    return TokenUsage(
        input_tokens=accumulated.input_tokens + current.input_tokens,
        output_tokens=accumulated.output_tokens + current.output_tokens,
    )


def _extract_evidence(entries: tuple[SessionEntry, ...]) -> CompactionEvidence:
    read_files: list[str] = []
    modified_files: list[str] = []
    commands: list[CompactionCommandEvidence] = []
    tool_errors: list[CompactionToolErrorEvidence] = []
    tool_calls: dict[str, ToolCall] = {}
    for entry in entries:
        message = _message_from_entry(entry)
        if isinstance(message, AssistantMessage):
            tool_calls.update((call.call_id, call) for call in message.tool_calls)
            continue
        if not isinstance(message, ToolResultMessage):
            continue
        for result in message.results:
            output = result.output or {}
            call = tool_calls.get(result.call_id)
            arguments = {} if call is None else call.arguments
            tool_name = result.tool_name if call is None else call.tool_name
            command = arguments.get("command", output.get("command"))
            cwd = arguments.get("cwd", output.get("cwd"))
            exit_status = output.get("exit_status", output.get("exit_code"))
            if tool_name in {"bash", "shell"} and isinstance(command, str):
                commands.append(
                    CompactionCommandEvidence(
                        command,
                        cwd if isinstance(cwd, str) else None,
                        exit_status if type(exit_status) is int else None,
                    )
                )
            path = arguments.get("path")
            if isinstance(path, str):
                if tool_name in {"read", "grep", "find", "ls"}:
                    read_files.append(path)
                elif tool_name in {"write", "edit"}:
                    modified_files.append(path)
            for key, target in (("read_files", read_files), ("modified_files", modified_files)):
                value = output.get(key)
                if isinstance(value, list):
                    target.extend(item for item in value if isinstance(item, str))
            if result.error is not None:
                tool_errors.append(CompactionToolErrorEvidence(result.call_id, result.error.code))
    return CompactionEvidence(
        tuple(dict.fromkeys(read_files)),
        tuple(dict.fromkeys(modified_files)),
        tuple(commands),
        tuple(tool_errors),
    )


def _merge_evidence(
    previous: CompactionEvidence,
    current: CompactionEvidence,
) -> CompactionEvidence:
    return CompactionEvidence(
        tuple(dict.fromkeys((*previous.read_files, *current.read_files))),
        tuple(dict.fromkeys((*previous.modified_files, *current.modified_files))),
        (*previous.commands, *current.commands),
        (*previous.tool_errors, *current.tool_errors),
    )
