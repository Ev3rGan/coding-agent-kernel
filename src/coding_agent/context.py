"""The single deterministic Model Context construction pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Protocol

from coding_agent.events import (
    AssistantMessage,
    TokenUsage,
    ToolCall,
    assistant_message_record,
)
from coding_agent.provider import (
    BranchSummaryMessage,
    ModelMessage,
    ProviderRequest,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.session import SessionEntry

ASSEMBLY_ORDER: Final = (
    "system_prompt",
    "active_tools",
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
    max_characters: int = 100_000

    def __post_init__(self) -> None:
        if self.max_characters <= 0:
            raise ValueError("max_characters must be positive")


@dataclass(frozen=True, slots=True)
class ContextInput:
    """A pure snapshot; it never exposes mutable Session or Agent Run state."""

    settings: ContextSettings
    active_branch: tuple[SessionEntry, ...] = ()
    active_tools: tuple[dict[str, object], ...] = ()
    injected_messages: tuple[ModelMessage, ...] = ()
    pending_messages: tuple[ModelMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """A validated checkpoint proposal for the Session to persist atomically."""

    covered_entry_ids: tuple[str, ...]
    summary: str
    version: int = 1


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
    raise ContextConstructionError(
        "context_entry_invalid",
        f"Message SessionEntry {entry.entry_id!r} has unsupported role {role!r}.",
        stage="context",
    )


def _request_record(request: ProviderRequest) -> dict[str, object]:
    def message_record(message: ModelMessage) -> dict[str, object]:
        if isinstance(message, ToolResultMessage):
            return {
                "role": message.role,
                "results": [
                    {
                        "call_id": result.call_id,
                        "tool_name": result.tool_name,
                        "status": result.status,
                        "output": result.output,
                        "error": (
                            None
                            if result.error is None
                            else {
                                "code": result.error.code,
                                "message": result.error.message,
                            }
                        ),
                    }
                    for result in message.results
                ],
            }
        if isinstance(message, AssistantMessage):
            return assistant_message_record(message)
        return {"role": message.role, "text": message.text}

    return {
        "system_prompt": request.system_prompt,
        "tools": list(request.tools),
        "tool_guidelines": request.tool_guidelines,
        "project_context": list(request.project_context),
        "messages": [message_record(message) for message in request.messages],
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
    """Authoritative projection from immutable runtime inputs to ProviderRequest."""

    def __init__(self, summarizer: BranchSummarizer | None = None) -> None:
        self._summarizer = summarizer or DeterministicBranchSummarizer()

    def build(self, context_input: ContextInput) -> ContextBuildResult:
        _ = context_input.pending_messages  # pending Agent Run state is never model input
        branch_messages = self._project_active_branch(context_input.active_branch)
        request = ProviderRequest(
            messages=branch_messages + context_input.injected_messages,
            tools=context_input.active_tools,
            system_prompt=context_input.settings.system_prompt,
            tool_guidelines=context_input.settings.tool_guidelines,
            project_context=context_input.settings.project_context,
        )
        estimated = estimate_provider_request_characters(request)
        compaction = None
        if estimated > context_input.settings.max_characters:
            covered_entry_ids = tuple(
                entry.entry_id
                for entry in context_input.active_branch
                if entry.kind != "configuration"
            )
            if not branch_messages or not covered_entry_ids:
                raise ContextConstructionError(
                    "context_budget_exceeded",
                    "Model Context exceeds its character budget and has no branch "
                    "history to compact.",
                    stage="context",
                )
            try:
                summary = self._summarizer.summarize(branch_messages)
            except Exception as exc:
                raise ContextConstructionError(
                    "compaction_summary_failed",
                    f"Compaction summary failed: {type(exc).__name__}: {exc}",
                    stage="compaction",
                ) from exc
            if not summary:
                raise ContextConstructionError(
                    "compaction_summary_invalid",
                    "Compaction summary must not be empty.",
                    stage="compaction",
                )
            compaction = CompactionPlan(covered_entry_ids, summary)
            request = ProviderRequest(
                messages=(
                    BranchSummaryMessage(
                        text=summary,
                    ),
                    *context_input.injected_messages,
                ),
                tools=context_input.active_tools,
                system_prompt=context_input.settings.system_prompt,
                tool_guidelines=context_input.settings.tool_guidelines,
                project_context=context_input.settings.project_context,
            )
            estimated = estimate_provider_request_characters(request)
            if estimated > context_input.settings.max_characters:
                raise ContextConstructionError(
                    "context_budget_exceeded",
                    "Compacted Model Context still exceeds its character budget.",
                    stage="context",
                )
        return ContextBuildResult(
            ModelContext(
                provider_request=request,
                estimated_characters=estimated,
                max_characters=context_input.settings.max_characters,
            ),
            compaction,
        )

    @staticmethod
    def _project_active_branch(
        active_branch: tuple[SessionEntry, ...],
    ) -> tuple[ModelMessage, ...]:
        checkpoint_index: int | None = None
        for index, entry in enumerate(active_branch):
            if entry.kind == "compaction":
                checkpoint_index = index

        projected: list[ModelMessage] = []
        start = 0
        if checkpoint_index is not None:
            checkpoint = active_branch[checkpoint_index]
            payload = checkpoint.payload
            summary = payload.get("summary")
            raw_coverage = payload.get("covered_entry_ids")
            expected_coverage = tuple(
                entry.entry_id
                for entry in active_branch[:checkpoint_index]
                if entry.kind != "configuration"
            )
            if (
                payload.get("version") != 1
                or not isinstance(summary, str)
                or not summary
                or not isinstance(raw_coverage, list)
                or not all(isinstance(item, str) for item in raw_coverage)
                or tuple(raw_coverage) != expected_coverage
            ):
                raise ContextConstructionError(
                    "compaction_checkpoint_invalid",
                    f"Compaction checkpoint {checkpoint.entry_id!r} is invalid.",
                    stage="context",
                )
            projected.append(
                BranchSummaryMessage(
                    text=summary,
                )
            )
            start = checkpoint_index + 1

        projected.extend(
            message
            for entry in active_branch[start:]
            if (message := _message_from_entry(entry)) is not None
        )
        return tuple(projected)
