from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from coding_agent import (
    AgentEventKind,
    AgentKernel,
    AgentRun,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    AgentSettledHookInput,
    AssistantMessage,
    BeforeAgentStartHookInput,
    Block,
    BranchSummaryMessage,
    CompactionHookInput,
    CompactionPlan,
    ContextHookInput,
    ContextPipeline,
    ContextSettings,
    ContextSupplement,
    ExtensionEventKind,
    ExtensionRegistrationError,
    ExtensionRegistry,
    FakeProvider,
    Hook,
    InMemorySessionStore,
    InputHookInput,
    LifecycleHookInput,
    LocalCodingEnvironment,
    MessageHookInput,
    ModelContext,
    Observe,
    ProviderDone,
    ProviderError,
    ProviderRequest,
    ProviderRequestHookInput,
    ProviderRequestSupplement,
    ProviderResponseHookInput,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    RetryPolicy,
    Session,
    SessionEntryDraft,
    SessionHookInput,
    SessionStateError,
    Supplement,
    ToolCall,
    ToolCallHookInput,
    ToolExecutionHookInput,
    ToolOutput,
    ToolResult,
    ToolResultHookInput,
    ToolResultMessage,
    ToolResultSupplement,
    ToolRuntime,
    ToolSpec,
    Transform,
    UserMessage,
    hook_policy,
)
from coding_agent.session import PersistenceRecord


class _InputExtension:
    def __init__(self, name: str, suffix: str) -> None:
        self.name = name
        self._suffix = suffix

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._transform_input)

    def _transform_input(self, hook_input: InputHookInput) -> Transform[str]:
        return Transform(hook_input.prompt + self._suffix)


def test_input_transforms_compose_in_extension_registration_order() -> None:
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        extensions=(
            _InputExtension("first", "-one"),
            _InputExtension("second", "-two"),
        ),
    )

    async def run_once() -> None:
        run = kernel.create_run("start")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())

    input_message = provider.requests[0].messages[-1]
    assert isinstance(input_message, UserMessage)
    assert input_message.text == "start-one-two"
    events = kernel.drain_extension_events()
    outcomes = [
        event
        for event in events
        if event.kind is ExtensionEventKind.HANDLER_OUTCOME and event.hook is Hook.INPUT
    ]
    assert [(event.extension_name, event.outcome) for event in outcomes] == [
        ("first", "transform"),
        ("second", "transform"),
    ]
    assert all(event.revalidated for event in outcomes)
    assert all(not isinstance(event, Observe) for event in events)


def test_fixed_hook_policies_publish_the_complete_result_algebra() -> None:
    mutable = ("observe", "transform", "block")
    supplementable = (*mutable, "supplement")
    expected = {
        Hook.INPUT: mutable,
        Hook.BEFORE_AGENT_START: ("observe", "block"),
        Hook.CONTEXT: supplementable,
        Hook.PROVIDER_REQUEST: supplementable,
        Hook.PROVIDER_RESPONSE: mutable,
        Hook.AGENT_START: ("observe",),
        Hook.AGENT_END: ("observe",),
        Hook.TURN_START: ("observe",),
        Hook.TURN_END: ("observe",),
        Hook.MESSAGE_START: ("observe",),
        Hook.MESSAGE_UPDATE: ("observe",),
        Hook.MESSAGE_END: mutable,
        Hook.TOOL_CALL: mutable,
        Hook.TOOL_RESULT: supplementable,
        Hook.TOOL_EXECUTION_START: ("observe",),
        Hook.TOOL_EXECUTION_UPDATE: ("observe",),
        Hook.TOOL_EXECUTION_END: ("observe",),
        Hook.SESSION_CONFIGURATION: ("observe",),
        Hook.SESSION_ENTRY: ("observe",),
        Hook.SESSION_RESUMED: ("observe",),
        Hook.SESSION_TREE: ("observe",),
        Hook.COMPACTION_START: mutable,
        Hook.COMPACTION_END: ("observe",),
        Hook.COMPACTION_FAILED: ("observe",),
        Hook.AGENT_SETTLED: ("observe", "supplement"),
    }
    assert {hook: hook_policy(hook).outcomes for hook in Hook} == expected


@pytest.mark.parametrize(
    ("hook", "expected_error"),
    [
        (Hook.INPUT, "extension_input_blocked"),
        (Hook.CONTEXT, "extension_context_blocked"),
        (Hook.PROVIDER_REQUEST, "extension_provider_blocked"),
        (Hook.PROVIDER_RESPONSE, "extension_provider_blocked"),
        (Hook.MESSAGE_END, "extension_message_blocked"),
    ],
)
def test_allowed_block_outcomes_reach_each_authoritative_production_path(
    hook: Hook,
    expected_error: str,
) -> None:
    kernel = AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        extensions=(_BlockingHookExtension(hook),),
    )

    async def run_once() -> str | None:
        run = kernel.create_run(f"block {hook.value}")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == expected_error
    assert any(
        event.kind is ExtensionEventKind.DISPATCH_BLOCKED
        and event.hook is hook
        and event.code == "matrix_block"
        for event in kernel.drain_extension_events()
    )


def test_legal_context_transform_and_provider_request_supplements_revalidate_in_order() -> None:
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        extensions=(
            _ContextTransformExtension(),
            _ProviderRequestSupplementExtension("request-one", "ONE"),
            _ProviderRequestSupplementExtension("request-two", "TWO"),
        ),
    )

    async def run_once() -> None:
        run = kernel.create_run("legal transform and supplements")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    assert provider.requests[0].system_prompt == "context transformed"
    assert provider.requests[0].project_context == ("ONE", "TWO")
    changes = [
        event
        for event in kernel.drain_extension_events()
        if event.kind is ExtensionEventKind.HANDLER_OUTCOME
        and event.outcome in {"transform", "supplement"}
    ]
    assert [(event.hook, event.outcome) for event in changes] == [
        (Hook.CONTEXT, "transform"),
        (Hook.PROVIDER_REQUEST, "supplement"),
        (Hook.PROVIDER_REQUEST, "supplement"),
    ]
    assert all(event.revalidated for event in changes)


class _EchoTool:
    spec = ToolSpec(
        name="extension_echo",
        description="Echo one value through the real ToolRuntime.",
        schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        mode="parallel",
        enabled_by_default=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del environment, cancel_event
        if on_progress is not None:
            await on_progress("extension", "echo progress")
        return ToolOutput({"echo": arguments["value"]})


class _ToolExtension:
    name = "tool-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(_EchoTool())


class _ConflictingReadTool(_EchoTool):
    spec = ToolSpec(
        name="read",
        description="Must not replace the built-in Tool.",
        schema=_EchoTool.spec.schema,
        mode="parallel",
        enabled_by_default=False,
    )


class _ConflictingToolExtension:
    name = "conflicting-tools"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(_EchoTool())
        registry.register_tool(_ConflictingReadTool())


class _RetainedRegistryExtension:
    name = "retained-registry"

    def __init__(self) -> None:
        self.registry: ExtensionRegistry | None = None

    def register(self, registry: ExtensionRegistry) -> None:
        self.registry = registry


class _InvalidSchemaTool(_EchoTool):
    spec = ToolSpec(
        name="invalid_schema",
        description="Invalid Extension Tool schema.",
        schema={
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer", "minimum": "zero"}},
            "additionalProperties": False,
        },
        mode="parallel",
        enabled_by_default=False,
    )


class _InvalidToolExtension:
    name = "invalid-tool"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(_InvalidSchemaTool())


class _SynchronousTool(_EchoTool):
    spec = ToolSpec(
        name="synchronous_extension_tool",
        description="Invalid synchronous implementation.",
        schema=_EchoTool.spec.schema,
        mode="parallel",
        enabled_by_default=False,
    )

    def execute(  # type: ignore[override]
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del arguments, environment, cancel_event, on_progress
        return ToolOutput({"invalid": True})


class _SynchronousToolExtension:
    name = "synchronous-tool-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(_SynchronousTool())  # type: ignore[arg-type]


class _MutableSchemaTool(_EchoTool):
    def __init__(self) -> None:
        self.spec = ToolSpec(
            name="mutable_schema_tool",
            description="Schema must be captured at registration.",
            schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
                "additionalProperties": False,
            },
            mode="parallel",
            enabled_by_default=False,
        )


class _SingleToolExtension:
    def __init__(self, name: str, tool: Any) -> None:
        self.name = name
        self._tool = tool

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(self._tool)


class _UnsupportedTopLevelSchemaTool(_EchoTool):
    spec = ToolSpec(
        name="unsupported_top_level_schema",
        description="Unsupported top-level constraint.",
        schema={
            "type": "object",
            "required": [],
            "properties": {},
            "additionalProperties": False,
            "minProperties": 1,
        },
        mode="parallel",
        enabled_by_default=False,
    )


class _RaisingExtensionTool(_EchoTool):
    spec = ToolSpec(
        name="raising_extension_tool",
        description="Raise deterministically for aligned error normalization.",
        schema={
            "type": "object",
            "required": [],
            "properties": {},
            "additionalProperties": False,
        },
        mode="parallel",
        enabled_by_default=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del arguments, environment, cancel_event, on_progress
        raise RuntimeError("extension tool exploded")


class _AlignedToolsExtension:
    name = "aligned-tools-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(_EchoTool())
        registry.register_tool(_RaisingExtensionTool())


class _CancellationMutatingTool(_EchoTool):
    spec = ToolSpec(
        name="cancellation_mutator",
        description="Attempt to mutate the Run-owned cancellation signal.",
        schema={
            "type": "object",
            "required": [],
            "properties": {},
            "additionalProperties": False,
        },
        mode="sequential",
        enabled_by_default=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del arguments, environment, on_progress
        assert cancel_event is not None
        if not isinstance(cancel_event, asyncio.Event):
            raise RuntimeError("cancellation view is not Event-compatible")
        cancel_event.set()
        return ToolOutput({"mutated": True})


class _ArgumentMutatingTool(_EchoTool):
    spec = ToolSpec(
        name="argument_mutator",
        description="Attempt to retain and mutate authoritative arguments.",
        schema=_EchoTool.spec.schema,
        mode="parallel",
        enabled_by_default=False,
    )

    def __init__(self) -> None:
        self.retained: dict[str, Any] | None = None

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del environment, cancel_event, on_progress
        self.retained = arguments
        arguments["value"] = "mutated-by-tool"
        return ToolOutput({"observed": arguments["value"]})


class _MalformedOutputTool(_EchoTool):
    spec = ToolSpec(
        name="malformed_output_tool",
        description="Return a non-JSON nested value.",
        schema={
            "type": "object",
            "required": [],
            "properties": {},
            "additionalProperties": False,
        },
        mode="parallel",
        enabled_by_default=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del arguments, environment, cancel_event, on_progress
        return ToolOutput({"invalid": object()})


class _MalformedProgressTool(_EchoTool):
    spec = ToolSpec(
        name="malformed_progress_tool",
        description="Publish invalid progress values.",
        schema={
            "type": "object",
            "required": [],
            "properties": {},
            "additionalProperties": False,
        },
        mode="parallel",
        enabled_by_default=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del arguments, environment, cancel_event
        assert on_progress is not None
        await on_progress(7, object())
        return ToolOutput({"invalid": True})


class _BlockingAfterProgressTool:
    spec = ToolSpec(
        name="blocking_after_progress",
        description="Block after one progress event for deterministic cleanup testing.",
        schema={
            "type": "object",
            "required": [],
            "properties": {},
            "additionalProperties": False,
        },
        mode="parallel",
        enabled_by_default=False,
    )

    def __init__(self) -> None:
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Any,
    ) -> ToolOutput:
        del arguments, environment, cancel_event
        try:
            assert on_progress is not None
            await on_progress("extension", "before observer failure")
            await self.release.wait()
            return ToolOutput({"released": True})
        finally:
            self.finished.set()


class _FailingProgressObserverExtension:
    name = "failing-progress-observer"

    def __init__(self, tool: _BlockingAfterProgressTool) -> None:
        self._tool = tool

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(self._tool)
        registry.register_hook(Hook.TOOL_EXECUTION_UPDATE, self._fail)

    def _fail(self, hook_input: object) -> Observe:
        del hook_input
        raise RuntimeError("progress observer failed")


class _InvalidBlockExtension:
    name = "invalid-block"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._invalid)

    def _invalid(self, hook_input: InputHookInput) -> Block:
        del hook_input
        return Block("", "")


class _InvalidSettledDraftExtension:
    name = "invalid-settled-draft"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.AGENT_SETTLED, self._invalid)

    def _invalid(
        self,
        hook_input: AgentSettledHookInput,
    ) -> Supplement[SessionEntryDraft]:
        del hook_input
        return Supplement(SessionEntryDraft("audit", 7))  # type: ignore[arg-type]


class _BlockingHookExtension:
    def __init__(self, hook: Hook) -> None:
        self.name = f"block-{hook.value}"
        self._hook = hook

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(self._hook, self._block)

    def _block(self, hook_input: object) -> Block:
        del hook_input
        return Block("matrix_block", f"blocked {self._hook.value}")


class _ProviderRequestSupplementExtension:
    def __init__(self, name: str, marker: str) -> None:
        self.name = name
        self._marker = marker

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_REQUEST, self._supplement)

    def _supplement(
        self,
        hook_input: ProviderRequestHookInput,
    ) -> Supplement[ProviderRequestSupplement]:
        del hook_input
        return Supplement(ProviderRequestSupplement(project_context=(self._marker,)))


class _ContextTransformExtension:
    name = "context-transform"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT, self._transform)

    def _transform(self, hook_input: ContextHookInput) -> Transform[ModelContext]:
        context = hook_input.context
        request = context.provider_request
        return Transform(
            ModelContext(
                ProviderRequest(
                    messages=request.messages,
                    tools=request.tools,
                    system_prompt="context transformed",
                    tool_guidelines=request.tool_guidelines,
                    project_context=request.project_context,
                ),
                estimated_characters=0,
                max_characters=context.max_characters,
                assembly_order=context.assembly_order,
            )
        )


class _ToolCallExtension:
    name = "tool-call-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._intercept)

    def _intercept(self, hook_input: ToolCallHookInput) -> Transform[ToolCall] | Block | Observe:
        arguments = hook_input.arguments
        if arguments.get("value") == "blocked":
            return Block("policy_block", "blocked by the example Extension")
        if isinstance(arguments.get("value"), str):
            return Transform(
                ToolCall(
                    hook_input.call_id,
                    hook_input.tool_name,
                    {"value": f"transformed:{arguments['value']}"},
                )
            )
        return Observe()


class _RetainedToolCallExtension:
    name = "retained-tool-call"

    def __init__(self) -> None:
        self.retained_arguments: dict[str, Any] | None = None

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._transform)
        registry.register_hook(Hook.TOOL_EXECUTION_START, self._mutate_retained)

    def _transform(self, hook_input: ToolCallHookInput) -> Transform[ToolCall]:
        self.retained_arguments = dict(hook_input.arguments)
        return Transform(
            ToolCall(
                hook_input.call_id,
                hook_input.tool_name,
                self.retained_arguments,
            )
        )

    def _mutate_retained(self, hook_input: object) -> Observe:
        del hook_input
        assert self.retained_arguments is not None
        self.retained_arguments["value"] = "late mutation"
        return Observe()


class _ToolResultTransformExtension:
    name = "tool-result-transform"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_RESULT, self._transform)

    def _transform(self, hook_input: ToolResultHookInput) -> Transform[ToolResult]:
        result = hook_input.result
        output = dict(result.output or {})
        output["transformed"] = True
        return Transform(
            ToolResult(
                result.call_id,
                result.tool_name,
                result.status,
                output,
                result.error,
            )
        )


class _ToolResultSupplementExtension:
    name = "tool-result-supplement"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_RESULT, self._supplement)

    def _supplement(self, hook_input: ToolResultHookInput) -> Supplement[ToolResultSupplement]:
        assert hook_input.result.output == {"echo": "hello", "transformed": True}
        return Supplement(ToolResultSupplement(output={"supplemented": "second"}))


class _ContextExtension:
    def __init__(self, name: str, marker: str) -> None:
        self.name = name
        self._marker = marker

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT, self._supplement)

    def _supplement(self, hook_input: ContextHookInput) -> Supplement[ContextSupplement]:
        assert hook_input.context.bounded
        return Supplement(ContextSupplement(project_context=(self._marker,)))


class _ProviderLifecycleExtension:
    name = "provider-lifecycle"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_REQUEST, self._request)
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._response)
        registry.register_hook(Hook.MESSAGE_END, self._message_end)

    def _request(self, hook_input: ProviderRequestHookInput) -> Transform[ProviderRequest]:
        request = hook_input.request
        return Transform(
            ProviderRequest(
                messages=request.messages,
                tools=request.tools,
                system_prompt="extension system",
                tool_guidelines=request.tool_guidelines,
                project_context=request.project_context,
            )
        )

    def _response(
        self, hook_input: ProviderResponseHookInput
    ) -> Transform[ProviderStreamEvent] | Observe:
        if isinstance(hook_input.event, ProviderTextDelta):
            return Transform(ProviderTextDelta(hook_input.event.delta.upper()))
        return Observe()

    def _message_end(self, hook_input: MessageHookInput) -> Transform[AssistantMessage]:
        message = hook_input.message
        return Transform(
            AssistantMessage(
                text=message.text + "!",
                thinking=message.thinking,
                tool_calls=message.tool_calls,
                usage=message.usage,
                stop_reason=message.stop_reason,
                response_id=message.response_id,
            )
        )


class _ClosingProvider:
    def __init__(self, scripts: tuple[tuple[ProviderStreamEvent, ...], ...]) -> None:
        self._scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.closed_attempts: list[int] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        attempt = len(self.requests) + 1
        self.requests.append(request)
        try:
            for event in self._scripts[min(attempt - 1, len(self._scripts) - 1)]:
                yield event
        finally:
            self.closed_attempts.append(attempt)


class _FailingCloseProvider:
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        del request
        try:
            yield ProviderTextDelta("partial")
            yield ProviderDone()
        finally:
            raise RuntimeError("provider close failed")


class _RetryAfterFailingCloseProvider:
    def __init__(self) -> None:
        self.attempts = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        del request
        self.attempts += 1
        if self.attempts == 1:
            try:
                yield ProviderError("provider_unavailable", "retry primary")
            finally:
                raise RuntimeError("secondary close failure")
        else:
            yield ProviderTextDelta("recovered")
            yield ProviderDone()


class _FailingProviderResponseExtension:
    name = "failing-provider-response"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._fail)

    def _fail(self, hook_input: ProviderResponseHookInput) -> Observe:
        del hook_input
        raise RuntimeError("provider response observer failed")


class _FailingMessageUpdateExtension:
    name = "failing-message-update"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.MESSAGE_UPDATE, self._fail)

    def _fail(self, hook_input: MessageHookInput) -> Observe:
        del hook_input
        raise RuntimeError("message update observer failed")


class _MalformedToolArgumentsExtension:
    name = "malformed-tool-arguments"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._transform)

    def _transform(
        self, hook_input: ProviderResponseHookInput
    ) -> Transform[ProviderStreamEvent] | Observe:
        if isinstance(hook_input.event, ProviderToolCallDelta):
            event = hook_input.event
            return Transform(
                ProviderToolCallDelta(
                    event.index,
                    call_id_delta=event.call_id_delta,
                    tool_name_delta=event.tool_name_delta,
                    arguments_delta="{",
                )
            )
        return Observe()


class _EraseToolCallIdExtension:
    name = "erase-tool-call-id"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._transform)

    def _transform(
        self, hook_input: ProviderResponseHookInput
    ) -> Transform[ProviderStreamEvent] | Observe:
        if isinstance(hook_input.event, ProviderToolCallDelta):
            event = hook_input.event
            return Transform(
                ProviderToolCallDelta(
                    event.index,
                    call_id_delta="",
                    tool_name_delta=event.tool_name_delta,
                    arguments_delta=event.arguments_delta,
                )
            )
        return Observe()


class _HarmlessProviderTextExtension:
    name = "harmless-provider-text"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._transform)

    def _transform(
        self, hook_input: ProviderResponseHookInput
    ) -> Transform[ProviderStreamEvent] | Observe:
        if isinstance(hook_input.event, ProviderTextDelta):
            return Transform(ProviderTextDelta(hook_input.event.delta.upper()))
        return Observe()


class _InvalidMessageRoleExtension:
    name = "invalid-message-role"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.MESSAGE_END, self._invalid)

    def _invalid(self, hook_input: MessageHookInput) -> Transform[AssistantMessage]:
        message = hook_input.message
        return Transform(
            AssistantMessage(
                role="user",  # type: ignore[arg-type]
                text=message.text,
                thinking=message.thinking,
                tool_calls=message.tool_calls,
                usage=message.usage,
                stop_reason=message.stop_reason,
                response_id=message.response_id,
            )
        )


class _DuplicateMessageToolCallExtension:
    name = "duplicate-message-tool-call"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.MESSAGE_END, self._duplicate)

    def _duplicate(self, hook_input: MessageHookInput) -> Transform[AssistantMessage] | Observe:
        message = hook_input.message
        if len(message.tool_calls) != 2:
            return Observe()
        first, second = message.tool_calls
        return Transform(
            AssistantMessage(
                text=message.text,
                thinking=message.thinking,
                tool_calls=(
                    first,
                    ToolCall(first.call_id, second.tool_name, second.arguments),
                ),
                usage=message.usage,
                stop_reason=message.stop_reason,
                response_id=message.response_id,
            )
        )


class _InvalidProviderEventExtension:
    name = "invalid-provider-event"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._invalid)

    def _invalid(
        self,
        hook_input: ProviderResponseHookInput,
    ) -> Transform[ProviderStreamEvent] | Observe:
        if isinstance(hook_input.event, ProviderTextDelta):
            return Transform(ProviderTextDelta(7))  # type: ignore[arg-type]
        return Observe()


class _ForgeProviderSuccessExtension:
    name = "forge-provider-success"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.PROVIDER_RESPONSE, self._forge)

    def _forge(
        self,
        hook_input: ProviderResponseHookInput,
    ) -> Transform[ProviderStreamEvent] | Observe:
        if isinstance(hook_input.event, ProviderError):
            return Transform(ProviderDone())
        return Observe()


class _ProviderExtension:
    name = "provider-extension"

    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_provider("extension-provider", self.provider)


class _LegacySessionStore:
    """Former append/load-only store used to prove no-op compatibility."""

    def __init__(self) -> None:
        self._delegate = InMemorySessionStore()

    def append(self, record: PersistenceRecord) -> None:
        self._delegate.append(record)

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]:
        return self._delegate.load(session_id)


def _validate_audit_entry(payload: Mapping[str, object]) -> None:
    if not isinstance(payload.get("note"), str):
        raise ValueError("audit.note must be a string")


class _SessionEntryExtension:
    name = "session-entry-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_session_entry_type("audit", _validate_audit_entry)


class _SettledEntryExtension:
    name = "settled-entry-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_session_entry_type("audit", _validate_audit_entry)
        registry.register_hook(Hook.AGENT_SETTLED, self._settled)

    def _settled(self, hook_input: AgentSettledHookInput) -> Supplement[SessionEntryDraft]:
        return Supplement(
            SessionEntryDraft(
                "audit",
                {"note": f"terminal:{hook_input.result.state.value}"},
            )
        )


class _TwoSettledEntriesExtension:
    name = "two-settled-entries"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_session_entry_type("audit", _validate_audit_entry)
        registry.register_hook(Hook.AGENT_SETTLED, self._first)
        registry.register_hook(Hook.AGENT_SETTLED, self._second)

    def _first(self, hook_input: AgentSettledHookInput) -> Supplement[SessionEntryDraft]:
        del hook_input
        return Supplement(SessionEntryDraft("audit", {"note": "first"}))

    def _second(self, hook_input: AgentSettledHookInput) -> Supplement[SessionEntryDraft]:
        del hook_input
        return Supplement(SessionEntryDraft("audit", {"note": "second"}))


class _TraceExtension:
    name = "trace-extension"

    def __init__(self) -> None:
        self.seen: list[tuple[Hook, object]] = []

    def register(self, registry: ExtensionRegistry) -> None:
        for hook in Hook:
            registry.register_hook(hook, self._handler_for(hook))

    def _handler_for(self, hook: Hook) -> Callable[[object], Observe]:
        def observe(hook_input: object) -> Observe:
            self.seen.append((hook, hook_input))
            return Observe()

        return observe


class _FailingAssistantEntryObserverExtension:
    name = "failing-assistant-entry-observer"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.SESSION_ENTRY, self._observe)

    def _observe(self, hook_input: SessionHookInput) -> Observe:
        if hook_input.entry is not None and hook_input.entry.payload.get("role") == "assistant":
            raise RuntimeError("assistant SessionEntry observer failed")
        return Observe()


class _FailingSummarizer:
    def summarize(self, messages: tuple[object, ...]) -> str:
        del messages
        raise RuntimeError("trace compaction failure")


class _CompactionTransformExtension:
    name = "compaction-transform"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.COMPACTION_START, self._transform)

    def _transform(self, hook_input: CompactionHookInput) -> Transform[CompactionPlan]:
        assert hook_input.plan is not None
        return Transform(
            CompactionPlan(
                hook_input.plan.covered_entry_ids,
                f"{hook_input.plan.summary} [extension]",
                hook_input.plan.version,
            )
        )


class _FailingInputExtension:
    name = "failing-input"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._fail)

    def _fail(self, hook_input: InputHookInput) -> Observe:
        del hook_input
        raise RuntimeError("invalid state mutation attempt")


class _BlockingStartExtension:
    name = "blocking-start"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.BEFORE_AGENT_START, self._block)

    def _block(self, hook_input: object) -> Block:
        del hook_input
        return Block("start_policy", "do not start this Agent Run")


class _TwoHandlerExtension:
    name = "two-handlers"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._first)
        registry.register_hook(Hook.INPUT, self._second)

    def _first(self, hook_input: InputHookInput) -> Transform[str]:
        return Transform(hook_input.prompt + "-first")

    def _second(self, hook_input: InputHookInput) -> Transform[str]:
        return Transform(hook_input.prompt + "-second")


class _BarrierProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        yield ProviderDone()


class _InvalidContextExtension:
    name = "invalid-context"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT, self._invalid)

    def _invalid(self, hook_input: ContextHookInput) -> Transform[object]:
        del hook_input
        return Transform({"mutable_kernel_state": True})


class _ContextBudgetEscalationExtension:
    name = "context-budget-escalation"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.CONTEXT, self._escalate)

    def _escalate(self, hook_input: ContextHookInput) -> Transform[ModelContext]:
        context = hook_input.context
        return Transform(
            ModelContext(
                provider_request=context.provider_request,
                estimated_characters=context.estimated_characters,
                max_characters=context.max_characters + 1,
                assembly_order=context.assembly_order,
            )
        )


class _SnapshotMutationExtension:
    name = "snapshot-mutation"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._mutate_copy)

    def _mutate_copy(self, hook_input: ToolCallHookInput) -> Observe:
        arguments = hook_input.arguments
        arguments["value"] = "mutated"
        return Observe()


class _ErrorToSuccessExtension:
    name = "error-to-success"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_RESULT, self._forge_success)

    def _forge_success(self, hook_input: ToolResultHookInput) -> Transform[ToolResult]:
        result = hook_input.result
        return Transform(
            ToolResult(
                result.call_id,
                result.tool_name,
                "success",
                {"forged": True},
            )
        )


def _tool_call_events(name: str, arguments: dict[str, Any]) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCallStart(0),
        ProviderToolCallDelta(
            0,
            call_id_delta="extension-call",
            tool_name_delta=name,
            arguments_delta=json.dumps(arguments),
        ),
        ProviderToolCallEnd(0),
        ProviderDone("tool_use"),
    )


def test_extension_tool_uses_existing_runtime_validation_and_result_path(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "hello"}),
            (ProviderTextDelta("observed extension result"), ProviderDone()),
        )
    )
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    kernel = AgentKernel(
        provider,
        tool_runtime=runtime,
        extensions=(_ToolExtension(),),
    )

    async def run_once() -> None:
        run = kernel.create_run("use extension tool")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())

    assert "extension_echo" in runtime.registered_names
    assert "extension_echo" in runtime.enabled_names
    tool_message = provider.requests[1].messages[-1]
    assert isinstance(tool_message, ToolResultMessage)
    assert tool_message.results[0].status == "success"
    assert tool_message.results[0].output == {"echo": "hello"}


def test_conflicting_capability_batch_is_rejected_without_partial_runtime_mutation(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    before = runtime.registered_names

    with pytest.raises(ExtensionRegistrationError, match="tool already registered: read"):
        AgentKernel(
            FakeProvider(((ProviderDone(),),)),
            tool_runtime=runtime,
            extensions=(_ConflictingToolExtension(),),
        )

    assert runtime.registered_names == before
    assert "extension_echo" not in runtime.registered_names

    store = InMemorySessionStore()
    with pytest.raises(ExtensionRegistrationError, match="tool already registered: read"):
        AgentKernel.with_new_session(
            FakeProvider(((ProviderDone(),),)),
            store,
            session_id="must-not-exist",
            configuration={"provider": "fake"},
            tool_runtime=runtime,
            extensions=(_ConflictingToolExtension(),),
        )
    assert store.load("must-not-exist") == ()


def test_registry_closes_after_explicit_registration_window() -> None:
    extension = _RetainedRegistryExtension()
    kernel = AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        extensions=(extension,),
    )
    kernel.drain_extension_events()
    assert extension.registry is not None

    with pytest.raises(ExtensionRegistrationError, match="registration window is closed"):
        extension.registry.register_hook(Hook.INPUT, lambda _: Observe())

    assert kernel.drain_extension_events() == ()


def test_invalid_extension_tool_schema_is_rejected_before_runtime_mutation(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    before = runtime.registered_names

    with pytest.raises(ExtensionRegistrationError, match="Tool schema"):
        AgentKernel(
            FakeProvider(((ProviderDone(),),)),
            tool_runtime=runtime,
            extensions=(_InvalidToolExtension(),),
        )

    assert runtime.registered_names == before


def test_synchronous_extension_tool_is_rejected_atomically(tmp_path: Path) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    before = runtime.registered_names

    with pytest.raises(ExtensionRegistrationError, match="async execute"):
        AgentKernel(
            FakeProvider(((ProviderDone(),),)),
            tool_runtime=runtime,
            extensions=(_SynchronousToolExtension(),),
        )

    assert runtime.registered_names == before


def test_registered_tool_schema_is_a_kernel_owned_snapshot(tmp_path: Path) -> None:
    tool = _MutableSchemaTool()
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        tool_runtime=runtime,
        extensions=(_SingleToolExtension("mutable-schema-extension", tool),),
    )

    tool.spec.schema["properties"]["value"]["type"] = "integer"
    exposed = next(spec for spec in runtime.schemas if spec.name == "mutable_schema_tool")
    exposed.schema["properties"]["value"]["type"] = "boolean"
    stable = next(spec for spec in runtime.schemas if spec.name == "mutable_schema_tool")

    assert stable.schema["properties"]["value"]["type"] == "string"


def test_unsupported_top_level_tool_schema_constraint_is_rejected(tmp_path: Path) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))

    with pytest.raises(ExtensionRegistrationError, match="unsupported constraints"):
        AgentKernel(
            FakeProvider(((ProviderDone(),),)),
            tool_runtime=runtime,
            extensions=(
                _SingleToolExtension(
                    "unsupported-schema-extension",
                    _UnsupportedTopLevelSchemaTool(),
                ),
            ),
        )


def test_extension_tool_exception_preserves_batch_alignment(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events("extension_echo", {"value": "ok"})[:-1],
                ProviderToolCallStart(1),
                ProviderToolCallDelta(
                    1,
                    call_id_delta="raising-call",
                    tool_name_delta="raising_extension_tool",
                    arguments_delta="{}",
                ),
                ProviderToolCallEnd(1),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_AlignedToolsExtension(),),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("aligned extension tool failure")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    feedback = provider.requests[1].messages[-1]
    assert isinstance(feedback, ToolResultMessage)
    assert [result.call_id for result in feedback.results] == ["extension-call", "raising-call"]
    assert feedback.results[0].status == "success"
    assert feedback.results[1].status == "error"
    assert feedback.results[1].error is not None
    assert feedback.results[1].error.code == "tool_error"


def test_extension_tool_cannot_mutate_run_cancellation_or_cancel_sibling(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            (
                ProviderToolCallStart(0),
                ProviderToolCallDelta(
                    0,
                    call_id_delta="cancel-mutator",
                    tool_name_delta="cancellation_mutator",
                    arguments_delta="{}",
                ),
                ProviderToolCallEnd(0),
                ProviderToolCallStart(1),
                ProviderToolCallDelta(
                    1,
                    call_id_delta="untouched-sibling",
                    tool_name_delta="extension_echo",
                    arguments_delta='{"value":"still runs"}',
                ),
                ProviderToolCallEnd(1),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(
            _SingleToolExtension("cancel-mutator-extension", _CancellationMutatingTool()),
            _ToolExtension(),
        ),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("protect Run cancellation")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    feedback = provider.requests[1].messages[-1]
    assert isinstance(feedback, ToolResultMessage)
    assert [result.call_id for result in feedback.results] == [
        "cancel-mutator",
        "untouched-sibling",
    ]
    assert feedback.results[0].status == "error"
    assert feedback.results[0].error is not None
    assert feedback.results[0].error.code == "tool_error"
    assert "read-only" in feedback.results[0].error.message
    assert feedback.results[1].status == "success"


def test_extension_tool_argument_mutation_cannot_change_authoritative_tool_call(
    tmp_path: Path,
) -> None:
    tool = _ArgumentMutatingTool()
    provider = FakeProvider(
        (
            (
                ProviderToolCallStart(0),
                ProviderToolCallDelta(
                    0,
                    call_id_delta="argument-mutator",
                    tool_name_delta="argument_mutator",
                    arguments_delta='{"value":"original"}',
                ),
                ProviderToolCallEnd(0),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_SingleToolExtension("argument-mutator-extension", tool),),
    )

    async def run_once() -> None:
        run = kernel.create_run("protect ToolCall arguments")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    first_message = provider.requests[0].messages[-1]
    assert isinstance(first_message, UserMessage)
    authoritative = next(
        message
        for message in provider.requests[1].messages
        if isinstance(message, AssistantMessage)
    )
    assert authoritative.tool_calls[0].arguments == {"value": "original"}
    assert tool.retained is not None
    tool.retained["value"] = "late-mutation"
    assert authoritative.tool_calls[0].arguments == {"value": "original"}


def test_malformed_extension_tool_output_becomes_safe_aligned_error(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            (
                *_tool_call_events("extension_echo", {"value": "ok"})[:-1],
                ProviderToolCallStart(1),
                ProviderToolCallDelta(
                    1,
                    call_id_delta="malformed-output",
                    tool_name_delta="malformed_output_tool",
                    arguments_delta="{}",
                ),
                ProviderToolCallEnd(1),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(
            _ToolExtension(),
            _SingleToolExtension("malformed-output-extension", _MalformedOutputTool()),
        ),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("normalize malformed output")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    feedback = provider.requests[1].messages[-1]
    assert isinstance(feedback, ToolResultMessage)
    assert [result.call_id for result in feedback.results] == [
        "extension-call",
        "malformed-output",
    ]
    assert feedback.results[0].status == "success"
    assert feedback.results[1].status == "error"
    assert feedback.results[1].output is None
    assert feedback.results[1].error is not None
    assert feedback.results[1].error.code == "tool_error"


def test_malformed_extension_tool_progress_is_rejected_before_public_event(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("malformed_progress_tool", {}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(
            _SingleToolExtension("malformed-progress-extension", _MalformedProgressTool()),
        ),
    )

    async def run_once() -> tuple[AgentRunState, list[AgentSessionEventKind]]:
        run = kernel.create_run("reject malformed progress")
        events = [event async for event in run]
        return (await run.result()).state, [event.kind for event in events]

    state, kinds = asyncio.run(run_once())
    assert state is AgentRunState.SETTLED
    assert AgentSessionEventKind.TOOL_EXECUTION_UPDATE not in kinds
    feedback = provider.requests[1].messages[-1]
    assert isinstance(feedback, ToolResultMessage)
    assert feedback.results[0].status == "error"
    assert feedback.results[0].error is not None
    assert feedback.results[0].error.code == "tool_error"


def test_session_constructor_rejects_invalid_custom_type_before_persistence() -> None:
    store = InMemorySessionStore()

    with pytest.raises(SessionStateError, match="named callables"):
        Session.create(
            store,
            session_id="invalid-entry-type-constructor",
            configuration={"provider": "fake"},
            entry_types={"": _validate_audit_entry},
        )

    assert store.load("invalid-entry-type-constructor") == ()


def test_tool_call_transform_revalidates_and_block_preserves_batch_alignment(
    tmp_path: Path,
) -> None:
    first_turn = (
        ProviderToolCallStart(0),
        ProviderToolCallDelta(
            0,
            call_id_delta="allowed",
            tool_name_delta="extension_echo",
            arguments_delta='{"value":"allowed"}',
        ),
        ProviderToolCallEnd(0),
        ProviderToolCallStart(1),
        ProviderToolCallDelta(
            1,
            call_id_delta="blocked",
            tool_name_delta="extension_echo",
            arguments_delta='{"value":"blocked"}',
        ),
        ProviderToolCallEnd(1),
        ProviderDone("tool_use"),
    )
    provider = FakeProvider(
        (first_turn, (ProviderTextDelta("observed aligned results"), ProviderDone()))
    )
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    kernel = AgentKernel(
        provider,
        tool_runtime=runtime,
        extensions=(_ToolExtension(), _ToolCallExtension()),
    )

    async def run_once() -> None:
        run = kernel.create_run("intercept tool calls")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())

    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert [result.call_id for result in message.results] == ["allowed", "blocked"]
    assert message.results[0].output == {"echo": "transformed:allowed"}
    assert message.results[1].status == "error"
    assert message.results[1].error is not None
    assert message.results[1].error.code == "extension_blocked"
    assert "policy_block" in message.results[1].error.message


def test_tool_result_transform_and_supplement_compose_before_provider_feedback(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "hello"}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(
            _ToolExtension(),
            _ToolResultTransformExtension(),
            _ToolResultSupplementExtension(),
        ),
    )

    async def run_once() -> list[ToolResult]:
        execution_results: list[ToolResult] = []
        run = kernel.create_run("tool result hooks")
        async for event in run:
            agent_event = event.agent_event
            if (
                agent_event is not None
                and agent_event.kind is AgentEventKind.TOOL_EXECUTION_END
                and agent_event.tool_result is not None
            ):
                execution_results.append(agent_event.tool_result)
        await run.result()
        return execution_results

    execution_results = asyncio.run(run_once())

    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert message.results[0].output == {
        "echo": "hello",
        "transformed": True,
        "supplemented": "second",
    }
    assert execution_results[0].output == message.results[0].output


def test_context_supplements_compose_in_registration_order_and_revalidate() -> None:
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        extensions=(
            _ContextExtension("context-one", "ONE"),
            _ContextExtension("context-two", "TWO"),
        ),
    )

    async def run_once() -> None:
        run = kernel.create_run("context input")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())

    assert provider.requests[0].project_context == ("ONE", "TWO")
    outcomes = [
        event
        for event in kernel.drain_extension_events()
        if event.kind is ExtensionEventKind.HANDLER_OUTCOME and event.hook is Hook.CONTEXT
    ]
    assert [(event.extension_name, event.outcome) for event in outcomes] == [
        ("context-one", "supplement"),
        ("context-two", "supplement"),
    ]
    assert all(event.revalidated for event in outcomes)


def test_provider_and_message_transforms_precede_authority_and_persistence() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "user", "assistant"))
    provider = FakeProvider(((ProviderTextDelta("provider text"), ProviderDone()),))
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        session_id="provider-hooks",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(_ProviderLifecycleExtension(),),
    )

    async def run_once() -> str:
        run = kernel.create_run("provider hooks")
        async for _ in run:
            pass
        result = await run.result()
        assert result.message is not None
        return result.message.text

    assert asyncio.run(run_once()) == "PROVIDER TEXT!"
    assert provider.requests[0].system_prompt == "extension system"
    assert kernel.session_active_branch[-1].payload["text"] == "PROVIDER TEXT!"


def test_message_transform_rejects_an_invalid_role_before_session_persistence() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "prompt", "malformed-authority"))
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderTextDelta("answer"), ProviderDone()),)),
        store,
        session_id="invalid-message-role",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(_InvalidMessageRoleExtension(),),
    )

    async def run_once() -> str | None:
        run = kernel.create_run("do not persist malformed authority")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_message_rejected"
    assert [entry.payload.get("role") for entry in kernel.session_active_branch] == [None, "user"]


def test_message_transform_duplicate_tool_call_ids_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            (
                ProviderToolCallStart(0),
                ProviderToolCallDelta(
                    0,
                    call_id_delta="first",
                    tool_name_delta="extension_echo",
                    arguments_delta='{"value":"one"}',
                ),
                ProviderToolCallEnd(0),
                ProviderToolCallStart(1),
                ProviderToolCallDelta(
                    1,
                    call_id_delta="second",
                    tool_name_delta="extension_echo",
                    arguments_delta='{"value":"two"}',
                ),
                ProviderToolCallEnd(1),
                ProviderDone("tool_use"),
            ),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_ToolExtension(), _DuplicateMessageToolCallExtension()),
    )

    async def run_once() -> tuple[str | None, list[AgentSessionEventKind]]:
        run = kernel.create_run("reject duplicate ToolCall IDs")
        events = [event async for event in run]
        result = await run.result()
        return (
            None if result.error is None else result.error.code,
            [event.kind for event in events],
        )

    error_code, kinds = asyncio.run(run_once())
    assert error_code == "extension_message_rejected"
    assert AgentSessionEventKind.TOOL_EXECUTION_START not in kinds
    assert len(provider.requests) == 1
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.MESSAGE_END
        for event in kernel.drain_extension_events()
    )


def test_provider_response_transform_revalidates_typed_event_fields() -> None:
    kernel = AgentKernel(
        FakeProvider(((ProviderTextDelta("answer"), ProviderDone()),)),
        extensions=(_InvalidProviderEventExtension(),),
    )

    async def run_once() -> tuple[str | None, str | None]:
        run = kernel.create_run("reject invalid provider event")
        async for _ in run:
            pass
        result = await run.result()
        if result.error is None:
            return None, None
        return result.error.code, result.error.source

    assert asyncio.run(run_once()) == ("extension_provider_rejected", "extension")
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.PROVIDER_RESPONSE
        for event in kernel.drain_extension_events()
    )


def test_provider_response_transform_cannot_turn_failure_into_success() -> None:
    kernel = AgentKernel(
        FakeProvider(((ProviderError("provider_unavailable", "must remain a failure"),),)),
        extensions=(_ForgeProviderSuccessExtension(),),
    )

    async def run_once() -> tuple[AgentRunState, str | None]:
        run = kernel.create_run("preserve provider failure ownership")
        async for _ in run:
            pass
        result = await run.result()
        return result.state, None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == (
        AgentRunState.FAILED,
        "extension_provider_rejected",
    )
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.PROVIDER_RESPONSE
        for event in kernel.drain_extension_events()
    )


def test_fixed_hook_set_dispatches_on_authoritative_runtime_paths(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "old-user", "old-answer"))
    session = Session.create(
        store,
        session_id="hook-trace",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 120)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 120))
    session.close()

    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "trace"}),
            (ProviderTextDelta("trace complete"), ProviderDone()),
        )
    )
    new_ids = iter(("checkpoint", "prompt", "assistant-tool", "assistant-final"))
    trace = _TraceExtension()
    kernel = AgentKernel.with_resumed_session(
        provider,
        store,
        "hook-trace",
        entry_id_factory=lambda: next(new_ids),
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        context_settings=ContextSettings(max_characters=2_500),
        extensions=(_ToolExtension(), trace),
    )

    async def run_once() -> None:
        run = kernel.create_run("trace all hooks")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())

    dispatched = {
        event.hook
        for event in kernel.drain_extension_events()
        if event.kind is ExtensionEventKind.DISPATCH_STARTED
    }
    assert dispatched == set(Hook) - {Hook.COMPACTION_FAILED}
    expected_input_types = {
        Hook.INPUT: InputHookInput,
        Hook.BEFORE_AGENT_START: BeforeAgentStartHookInput,
        Hook.CONTEXT: ContextHookInput,
        Hook.PROVIDER_REQUEST: ProviderRequestHookInput,
        Hook.PROVIDER_RESPONSE: ProviderResponseHookInput,
        Hook.AGENT_START: LifecycleHookInput,
        Hook.AGENT_END: LifecycleHookInput,
        Hook.TURN_START: LifecycleHookInput,
        Hook.TURN_END: LifecycleHookInput,
        Hook.MESSAGE_START: MessageHookInput,
        Hook.MESSAGE_UPDATE: MessageHookInput,
        Hook.MESSAGE_END: MessageHookInput,
        Hook.TOOL_CALL: ToolCallHookInput,
        Hook.TOOL_RESULT: ToolResultHookInput,
        Hook.TOOL_EXECUTION_START: ToolExecutionHookInput,
        Hook.TOOL_EXECUTION_UPDATE: ToolExecutionHookInput,
        Hook.TOOL_EXECUTION_END: ToolExecutionHookInput,
        Hook.SESSION_CONFIGURATION: SessionHookInput,
        Hook.SESSION_ENTRY: SessionHookInput,
        Hook.SESSION_RESUMED: SessionHookInput,
        Hook.SESSION_TREE: SessionHookInput,
        Hook.COMPACTION_START: CompactionHookInput,
        Hook.COMPACTION_END: CompactionHookInput,
        Hook.AGENT_SETTLED: AgentSettledHookInput,
    }
    first_inputs: dict[Hook, object] = {}
    for hook, hook_input in trace.seen:
        first_inputs.setdefault(hook, hook_input)
    assert set(first_inputs) == set(Hook) - {Hook.COMPACTION_FAILED}
    assert all(
        isinstance(first_inputs[hook], input_type)
        for hook, input_type in expected_input_types.items()
    )
    first_hook_order = list(dict.fromkeys(hook for hook, _ in trace.seen))
    assert first_hook_order == [
        Hook.SESSION_RESUMED,
        Hook.SESSION_CONFIGURATION,
        Hook.SESSION_TREE,
        Hook.INPUT,
        Hook.BEFORE_AGENT_START,
        Hook.COMPACTION_START,
        Hook.CONTEXT,
        Hook.SESSION_ENTRY,
        Hook.COMPACTION_END,
        Hook.AGENT_START,
        Hook.TURN_START,
        Hook.MESSAGE_START,
        Hook.PROVIDER_REQUEST,
        Hook.PROVIDER_RESPONSE,
        Hook.MESSAGE_UPDATE,
        Hook.MESSAGE_END,
        Hook.TOOL_CALL,
        Hook.TOOL_EXECUTION_START,
        Hook.TOOL_EXECUTION_UPDATE,
        Hook.TOOL_RESULT,
        Hook.TOOL_EXECUTION_END,
        Hook.TURN_END,
        Hook.AGENT_END,
        Hook.AGENT_SETTLED,
    ]


def test_agent_settled_supplements_registered_entry_before_unique_terminal() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "prompt", "answer", "settled-audit"))
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderTextDelta("done"), ProviderDone()),)),
        store,
        session_id="settled-entry",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(_SettledEntryExtension(),),
    )

    async def run_once() -> list[AgentSessionEventKind]:
        run = kernel.create_run("settle")
        events = [event async for event in run]
        await run.result()
        return [event.kind for event in events]

    kinds = asyncio.run(run_once())

    assert kernel.session_active_branch[-1].kind == "audit"
    assert kernel.session_active_branch[-1].payload == {"note": "terminal:settled"}
    assert kinds[-1] is AgentSessionEventKind.RUN_SETTLED
    assert AgentSessionEventKind.SESSION_ENTRY in kinds[-3:-1]
    assert all(kind.value not in {event.value for event in ExtensionEventKind} for kind in kinds)


def test_agent_settled_batch_failure_is_atomic_and_does_not_rewrite_terminal() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "prompt", "answer", "duplicate", "duplicate"))
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderTextDelta("done"), ProviderDone()),)),
        store,
        session_id="settled-batch-atomic",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(_TwoSettledEntriesExtension(),),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("settle atomically")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    assert all(entry.kind != "audit" for entry in kernel.session_active_branch)
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED
        and event.hook is Hook.AGENT_SETTLED
        and event.code == "settlement_persistence_failed"
        for event in kernel.drain_extension_events()
    )


def test_invalid_settled_draft_is_rejected_without_rewriting_terminal() -> None:
    kernel = AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        extensions=(_InvalidSettledDraftExtension(),),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("reject invalid settled draft")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.AGENT_SETTLED
        for event in kernel.drain_extension_events()
    )


def test_compaction_failure_hook_wraps_canonical_failure_without_history_damage() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "old-user", "old-answer"))
    session = Session.create(
        store,
        session_id="compaction-failure-hook",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 120)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 120))
    original_branch = session.active_branch
    session.drain_events()
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_pipeline=ContextPipeline(_FailingSummarizer()),
        context_settings=ContextSettings(max_characters=2_500),
        extensions=(_TraceExtension(),),
    )

    async def run_once() -> None:
        run = kernel.create_run("force summary failure")
        async for _ in run:
            pass
        result = await run.result()
        assert result.error is not None
        assert result.error.code == "compaction_summary_failed"

    asyncio.run(run_once())

    assert provider.requests == []
    assert session.active_branch == original_branch
    assert any(
        event.kind is ExtensionEventKind.DISPATCH_STARTED and event.hook is Hook.COMPACTION_FAILED
        for event in kernel.drain_extension_events()
    )


def test_compaction_transform_is_shared_by_current_request_and_persisted_checkpoint() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "old-user", "old-answer"))
    session = Session.create(
        store,
        session_id="compaction-transform",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 120)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 120))
    session.close()
    provider = FakeProvider(((ProviderDone(),),))
    new_ids = iter(("checkpoint", "prompt", "answer"))
    kernel = AgentKernel.with_resumed_session(
        provider,
        store,
        "compaction-transform",
        entry_id_factory=lambda: next(new_ids),
        context_settings=ContextSettings(max_characters=2_500),
        extensions=(_CompactionTransformExtension(),),
    )

    async def run_once() -> None:
        run = kernel.create_run("trigger transformed compaction")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    request_summary = provider.requests[0].messages[0]
    assert isinstance(request_summary, BranchSummaryMessage)
    checkpoint = next(entry for entry in kernel.session_active_branch if entry.kind == "compaction")
    assert request_summary.text == checkpoint.payload["summary"]
    assert request_summary.text.endswith("[extension]")


def test_compaction_block_prevents_provider_call_and_checkpoint_persistence() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "old-user", "old-answer"))
    session = Session.create(
        store,
        session_id="compaction-block",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 120)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 120))
    original_branch = session.active_branch
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        session=session,
        context_settings=ContextSettings(max_characters=2_500),
        extensions=(_BlockingHookExtension(Hook.COMPACTION_START),),
    )

    async def run_once() -> str | None:
        run = kernel.create_run("block compaction")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_compaction_blocked"
    assert provider.requests == []
    assert session.active_branch == original_branch


def test_input_handler_exception_becomes_failed_run_without_session_mutation() -> None:
    store = InMemorySessionStore()
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        session_id="input-failure",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: "root",
        extensions=(_FailingInputExtension(),),
    )
    kernel.drain_session_events()

    async def run_once() -> tuple[list[AgentSessionEventKind], str | None]:
        run = kernel.create_run("must not persist")
        events = [event async for event in run]
        result = await run.result()
        return [event.kind for event in events], None if result.error is None else result.error.code

    kinds, error_code = asyncio.run(run_once())

    assert error_code == "extension_input_rejected"
    assert kinds == [AgentSessionEventKind.ERROR, AgentSessionEventKind.RUN_FAILED]
    assert [entry.kind for entry in kernel.session_active_branch] == ["configuration"]
    assert any(
        event.kind is ExtensionEventKind.HANDLER_FAILED and event.hook is Hook.INPUT
        for event in kernel.drain_extension_events()
    )


def test_invalid_block_enters_rejection_algebra_instead_of_blocking() -> None:
    kernel = AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        extensions=(_InvalidBlockExtension(),),
    )

    async def run_once() -> str | None:
        run = kernel.create_run("reject malformed Block")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_input_rejected"
    extension_events = kernel.drain_extension_events()
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.INPUT
        for event in extension_events
    )
    assert all(event.kind is not ExtensionEventKind.DISPATCH_BLOCKED for event in extension_events)


def test_handlers_within_one_extension_keep_registration_order() -> None:
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(provider, extensions=(_TwoHandlerExtension(),))

    async def run_once() -> None:
        run = kernel.create_run("base")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[0].messages[-1]
    assert isinstance(message, UserMessage)
    assert message.text == "base-first-second"


def test_before_agent_start_block_precedes_context_and_session_mutation() -> None:
    store = InMemorySessionStore()
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        session_id="blocked-start",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: "root",
        extensions=(_BlockingStartExtension(),),
    )
    kernel.drain_session_events()

    async def run_once() -> str | None:
        run = kernel.create_run("must not enter Context")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_agent_start_blocked"
    assert provider.requests == []
    assert kernel.model_contexts == ()
    assert [entry.kind for entry in kernel.session_active_branch] == ["configuration"]


def test_extension_dispatch_preserves_retry_and_unique_settled_terminal() -> None:
    provider = FakeProvider(
        (
            (
                ProviderTextDelta("discarded"),
                ProviderError("provider_unavailable", "retry"),
            ),
            (ProviderTextDelta("recovered"), ProviderDone()),
        )
    )
    kernel = AgentKernel(
        provider,
        retry_policy=RetryPolicy(max_attempts=2),
        extensions=(_TraceExtension(),),
    )

    async def run_once() -> tuple[AgentRunState, list[AgentSessionEventKind]]:
        run = kernel.create_run("retry")
        events = [event async for event in run]
        result = await run.result()
        return result.state, [event.kind for event in events]

    state, kinds = asyncio.run(run_once())
    assert state is AgentRunState.SETTLED
    assert kinds.count(AgentSessionEventKind.PROVIDER_RETRY) == 1
    assert kinds.count(AgentSessionEventKind.RUN_SETTLED) == 1
    extension_events = kernel.drain_extension_events()
    assert (
        sum(
            event.kind is ExtensionEventKind.DISPATCH_STARTED
            and event.hook is Hook.PROVIDER_REQUEST
            for event in extension_events
        )
        == 1
    )
    assert provider.requests[0] == provider.requests[1]
    assert (
        sum(
            event.kind is ExtensionEventKind.DISPATCH_STARTED and event.hook is Hook.AGENT_SETTLED
            for event in extension_events
        )
        == 1
    )


@pytest.mark.parametrize(
    "extension",
    (
        _BlockingHookExtension(Hook.PROVIDER_RESPONSE),
        _FailingProviderResponseExtension(),
        _FailingMessageUpdateExtension(),
    ),
    ids=("response-block", "response-handler-failure", "message-observer-failure"),
)
def test_provider_iterator_closes_on_every_extension_early_exit(extension: object) -> None:
    provider = _ClosingProvider(((ProviderTextDelta("partial"), ProviderDone()),))
    kernel = AgentKernel(provider, extensions=(extension,))  # type: ignore[arg-type]

    async def run_once() -> None:
        run = kernel.create_run("close provider stream")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    assert provider.closed_attempts == [1]


def test_provider_iterator_closes_each_retry_attempt() -> None:
    provider = _ClosingProvider(
        (
            (ProviderError("provider_unavailable", "retry"),),
            (ProviderTextDelta("recovered"), ProviderDone()),
        )
    )
    kernel = AgentKernel(provider, retry_policy=RetryPolicy(max_attempts=2))

    async def run_once() -> AgentRunState:
        run = kernel.create_run("close retries")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    assert provider.closed_attempts == [1, 2]


def test_delayed_invalid_transformed_stream_is_rejected_with_handler_provenance() -> None:
    provider = FakeProvider((_tool_call_events("extension_echo", {"value": "valid"}),))
    kernel = AgentKernel(provider, extensions=(_MalformedToolArgumentsExtension(),))

    async def run_once() -> str | None:
        run = kernel.create_run("reject malformed transformed stream")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_provider_rejected"
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED
        and event.hook is Hook.PROVIDER_RESPONSE
        and event.extension_name == "malformed-tool-arguments"
        for event in kernel.drain_extension_events()
    )


def test_transformed_invalid_tool_identity_is_rejected_at_provider_response_source() -> None:
    provider = FakeProvider((_tool_call_events("extension_echo", {"value": "valid"}),))
    kernel = AgentKernel(provider, extensions=(_EraseToolCallIdExtension(),))

    async def run_once() -> tuple[str | None, str | None]:
        run = kernel.create_run("reject transformed ToolCall identity")
        async for _ in run:
            pass
        result = await run.result()
        if result.error is None:
            return None, None
        return result.error.code, result.error.source

    assert asyncio.run(run_once()) == ("extension_provider_rejected", "extension")
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED
        and event.hook is Hook.PROVIDER_RESPONSE
        and event.extension_name == "erase-tool-call-id"
        and event.code == "stream_contract_violation"
        for event in kernel.drain_extension_events()
    )


def test_raw_invalid_tool_identity_is_attributed_to_provider_without_extension_rejection() -> None:
    provider = FakeProvider(
        (
            (
                ProviderToolCallStart(0),
                ProviderToolCallDelta(
                    0,
                    call_id_delta="",
                    tool_name_delta="extension_echo",
                    arguments_delta='{"value":"raw invalid"}',
                ),
                ProviderToolCallEnd(0),
                ProviderDone("tool_use"),
            ),
        )
    )
    kernel = AgentKernel(provider)

    async def run_once() -> tuple[str | None, str | None]:
        run = kernel.create_run("attribute invalid raw ToolCall")
        async for _ in run:
            pass
        result = await run.result()
        if result.error is None:
            return None, None
        return result.error.code, result.error.source

    assert asyncio.run(run_once()) == ("provider_exception", "provider")
    assert not any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED
        for event in kernel.drain_extension_events()
    )


def test_delayed_stream_rejection_only_names_transformers_of_failed_component() -> None:
    provider = FakeProvider(
        (
            (
                ProviderTextDelta("harmless"),
                *_tool_call_events("extension_echo", {"value": "valid"}),
            ),
        )
    )
    kernel = AgentKernel(
        provider,
        extensions=(_HarmlessProviderTextExtension(), _MalformedToolArgumentsExtension()),
    )

    async def run_once() -> None:
        run = kernel.create_run("component provenance")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    rejected_names = {
        event.extension_name
        for event in kernel.drain_extension_events()
        if event.kind is ExtensionEventKind.OUTCOME_REJECTED
        and event.hook is Hook.PROVIDER_RESPONSE
        and event.code == "stream_contract_violation"
    }
    assert rejected_names == {"malformed-tool-arguments"}


def test_provider_cleanup_failure_is_secondary_and_observable() -> None:
    kernel = AgentKernel(
        _FailingCloseProvider(),
        extensions=(_BlockingHookExtension(Hook.PROVIDER_RESPONSE),),
    )

    async def run_once() -> tuple[AgentRunState, str | None, int]:
        run = kernel.create_run("preserve primary terminal")
        events = [event async for event in run]
        result = await run.result()
        return (
            result.state,
            None if result.error is None else result.error.code,
            sum(
                event.kind
                in {
                    AgentSessionEventKind.RUN_SETTLED,
                    AgentSessionEventKind.RUN_FAILED,
                    AgentSessionEventKind.RUN_CANCELLED,
                }
                for event in events
            ),
        )

    state, error_code, terminal_count = asyncio.run(run_once())
    assert state is AgentRunState.FAILED
    assert error_code == "extension_provider_blocked"
    assert terminal_count == 1
    assert any(
        event.kind is ExtensionEventKind.RUNTIME_FAILURE
        and event.hook is Hook.PROVIDER_RESPONSE
        and event.code == "provider_cleanup_failed"
        and event.extension_name is None
        for event in kernel.drain_extension_events()
    )


def test_provider_cleanup_failure_does_not_replace_retryable_provider_error() -> None:
    provider = _RetryAfterFailingCloseProvider()
    kernel = AgentKernel(provider, retry_policy=RetryPolicy(max_attempts=2))

    async def run_once() -> tuple[AgentRunState, list[AgentSessionEventKind]]:
        run = kernel.create_run("preserve retry primary")
        events = [event async for event in run]
        result = await run.result()
        return result.state, [event.kind for event in events]

    state, kinds = asyncio.run(run_once())
    assert state is AgentRunState.SETTLED
    assert provider.attempts == 2
    assert kinds.count(AgentSessionEventKind.PROVIDER_RETRY) == 1
    assert any(
        event.kind is ExtensionEventKind.RUNTIME_FAILURE and event.code == "provider_cleanup_failed"
        for event in kernel.drain_extension_events()
    )


def test_raw_provider_stream_failure_is_not_misattributed_to_prior_harmless_transform() -> None:
    provider = FakeProvider(
        (
            (
                ProviderTextDelta("harmless"),
                ProviderToolCallStart(0),
                ProviderToolCallDelta(
                    0,
                    call_id_delta="raw-invalid",
                    tool_name_delta="extension_echo",
                    arguments_delta="{",
                ),
                ProviderToolCallEnd(0),
            ),
        )
    )
    kernel = AgentKernel(provider, extensions=(_HarmlessProviderTextExtension(),))

    async def run_once() -> str | None:
        run = kernel.create_run("attribute raw provider failure")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "provider_exception"
    assert not any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.PROVIDER_RESPONSE
        for event in kernel.drain_extension_events()
    )


def test_extension_dispatch_preserves_cancel_and_joins_observer_task() -> None:
    async def scenario() -> None:
        provider = _BarrierProvider()
        kernel = AgentKernel(provider, extensions=(_TraceExtension(),))
        run = kernel.create_run("cancel")
        events_task = asyncio.create_task(_collect_events(run))
        await provider.started.wait()

        result = await run.cancel()
        events = await events_task

        assert events_task.done()
        assert result.state is AgentRunState.CANCELLED
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
        assert (
            sum(
                event.kind is ExtensionEventKind.DISPATCH_STARTED
                and event.hook is Hook.AGENT_SETTLED
                for event in kernel.drain_extension_events()
            )
            == 1
        )

    asyncio.run(scenario())


def test_observer_failure_closes_tool_source_and_joins_active_batch(tmp_path: Path) -> None:
    async def scenario() -> None:
        tool = _BlockingAfterProgressTool()
        provider = FakeProvider(
            (
                _tool_call_events("blocking_after_progress", {}),
                (ProviderDone(),),
            )
        )
        kernel = AgentKernel(
            provider,
            tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
            extensions=(_FailingProgressObserverExtension(tool),),
        )
        run = kernel.create_run("fail while a Tool batch is active")
        events = [event async for event in run]
        result = await run.result()

        assert result.state is AgentRunState.FAILED
        assert result.error is not None
        assert result.error.source == "extension"
        assert events[-1].kind is AgentSessionEventKind.RUN_FAILED
        assert tool.finished.is_set()

    asyncio.run(scenario())


def test_failing_session_observer_preserves_and_dispatches_all_committed_events() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "prompt", "answer"))
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderTextDelta("answer"), ProviderDone()),)),
        store,
        session_id="observer-event-preservation",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(_FailingAssistantEntryObserverExtension(),),
    )

    async def run_once() -> tuple[list[AgentSessionEvent], AgentRunState]:
        run = kernel.create_run("preserve Session events")
        events = [event async for event in run]
        return events, (await run.result()).state

    events, state = asyncio.run(run_once())
    assert state is AgentRunState.SETTLED
    assistant_entry_index = next(
        index
        for index, event in enumerate(events)
        if event.kind is AgentSessionEventKind.SESSION_ENTRY
        and event.session_entry is not None
        and event.session_entry.payload.get("role") == "assistant"
    )
    assert events[assistant_entry_index + 1].kind is AgentSessionEventKind.ACTIVE_BRANCH
    assert events[-1].kind is AgentSessionEventKind.RUN_SETTLED
    extension_events = kernel.drain_extension_events()
    failed_index = next(
        index
        for index, event in enumerate(extension_events)
        if event.kind is ExtensionEventKind.HANDLER_FAILED and event.hook is Hook.SESSION_ENTRY
    )
    assert any(
        event.kind is ExtensionEventKind.DISPATCH_STARTED and event.hook is Hook.SESSION_TREE
        for event in extension_events[failed_index + 1 :]
    )


def test_no_settled_supplements_do_not_require_transactional_store_extension() -> None:
    store = _LegacySessionStore()
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderDone(),),)),
        store,  # type: ignore[arg-type]
        session_id="legacy-no-supplements",
        configuration={"provider": "fake"},
        entry_id_factory=iter(("root", "prompt", "answer")).__next__,
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("no supplements")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    assert not any(
        event.code == "settlement_persistence_failed" for event in kernel.drain_extension_events()
    )


async def _collect_events(run: AgentRun) -> list[AgentSessionEvent]:
    return [event async for event in run]


def test_invalid_context_result_fails_before_provider_and_preserves_session() -> None:
    store = InMemorySessionStore()
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel.with_new_session(
        provider,
        store,
        session_id="invalid-context",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: "root",
        extensions=(_InvalidContextExtension(),),
    )
    kernel.drain_session_events()

    async def run_once() -> str | None:
        run = kernel.create_run("must not persist")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_context_rejected"
    assert provider.requests == []
    assert [entry.kind for entry in kernel.session_active_branch] == ["configuration"]
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.CONTEXT
        for event in kernel.drain_extension_events()
    )


def test_context_transform_cannot_raise_the_kernel_owned_budget() -> None:
    provider = FakeProvider(((ProviderDone(),),))
    kernel = AgentKernel(
        provider,
        context_settings=ContextSettings(max_characters=2_500),
        extensions=(_ContextBudgetEscalationExtension(),),
    )

    async def run_once() -> str | None:
        run = kernel.create_run("keep the canonical budget")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_context_rejected"
    assert provider.requests == []


def test_mutating_tool_call_snapshot_cannot_change_kernel_owned_arguments(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "original"}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_ToolExtension(), _SnapshotMutationExtension()),
    )

    async def run_once() -> None:
        run = kernel.create_run("snapshot")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert message.results[0].output == {"echo": "original"}


def test_retained_transform_reference_cannot_mutate_kernel_owned_tool_call(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "stable"}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_ToolExtension(), _RetainedToolCallExtension()),
    )

    async def run_once() -> None:
        run = kernel.create_run("retain then mutate")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert message.results[0].output == {"echo": "stable"}


def test_tool_result_cannot_forge_blocked_execution_as_success(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "blocked"}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(
            _ToolExtension(),
            _ToolCallExtension(),
            _ErrorToSuccessExtension(),
        ),
    )

    async def run_once() -> None:
        run = kernel.create_run("do not forge")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert message.results[0].status == "error"
    assert message.results[0].error is not None
    assert message.results[0].error.code == "extension_rejected"
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED and event.hook is Hook.TOOL_RESULT
        for event in kernel.drain_extension_events()
    )


def test_tool_result_block_becomes_an_aligned_structured_error(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "block result"}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_ToolExtension(), _BlockingHookExtension(Hook.TOOL_RESULT)),
    )

    async def run_once() -> None:
        run = kernel.create_run("block ToolResult")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert len(message.results) == 1
    assert message.results[0].status == "error"
    assert message.results[0].error is not None
    assert message.results[0].error.code == "extension_blocked"


def test_extension_provider_is_selected_without_a_second_provider_loop() -> None:
    provider = FakeProvider(((ProviderTextDelta("from extension provider"), ProviderDone()),))
    kernel = AgentKernel(
        "extension-provider",
        extensions=(_ProviderExtension(provider),),
    )

    async def run_once() -> None:
        run = kernel.create_run("provider input")
        async for _ in run:
            pass
        result = await run.result()
        assert result.message is not None
        assert result.message.text == "from extension provider"

    asyncio.run(run_once())

    assert len(provider.requests) == 1
    provider_input = provider.requests[0].messages[-1]
    assert isinstance(provider_input, UserMessage)
    assert provider_input.text == "provider input"


def test_custom_session_entry_type_persists_resumes_and_rejects_invalid_payload() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "audit-entry"))
    extension = _SessionEntryExtension()
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        session_id="extension-session",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(extension,),
    )

    entry = kernel.append_extension_entry("audit", {"note": "registered payload"})
    branch_before_invalid = kernel.session_active_branch
    try:
        kernel.append_extension_entry("audit", {"note": 7})
    except ValueError as exc:
        assert "audit.note" in str(exc)
    else:  # pragma: no cover - the test requires an explicit rejection
        raise AssertionError("invalid custom SessionEntry payload was accepted")
    assert kernel.session_active_branch == branch_before_invalid
    kernel.close_session()

    resumed = AgentKernel.with_resumed_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        "extension-session",
        extensions=(extension,),
    )

    assert entry.kind == "audit"
    assert resumed.session_active_branch[-1].kind == "audit"
    assert resumed.session_active_branch[-1].payload == {"note": "registered payload"}


def test_extension_registers_custom_entry_type_on_an_existing_session() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "audit-entry"))
    session = Session.create(
        store,
        session_id="existing-session",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    kernel = AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        session=session,
        extensions=(_SessionEntryExtension(),),
    )

    entry = kernel.append_extension_entry("audit", {"note": "installed atomically"})

    assert entry.kind == "audit"
    assert kernel.session_active_branch[-1] == entry


def test_session_and_tree_hooks_dispatch_at_public_kernel_boundaries_without_a_run() -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "audit-entry"))
    extensions = (_SessionEntryExtension(), _TraceExtension())
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        session_id="public-session-hooks",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=extensions,
    )

    created_hooks = {
        event.hook
        for event in kernel.drain_extension_events()
        if event.kind is ExtensionEventKind.DISPATCH_STARTED
    }
    assert {
        Hook.SESSION_CONFIGURATION,
        Hook.SESSION_ENTRY,
        Hook.SESSION_TREE,
    }.issubset(created_hooks)

    kernel.append_extension_entry("audit", {"note": "public append"})
    appended_hooks = [
        event.hook
        for event in kernel.drain_extension_events()
        if event.kind is ExtensionEventKind.DISPATCH_STARTED
    ]
    assert appended_hooks == [Hook.SESSION_ENTRY, Hook.SESSION_TREE]

    kernel.fork_session("root")
    assert any(
        event.kind is ExtensionEventKind.DISPATCH_STARTED and event.hook is Hook.SESSION_TREE
        for event in kernel.drain_extension_events()
    )
    kernel.close_session()

    resumed = AgentKernel.with_resumed_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        "public-session-hooks",
        extensions=extensions,
    )
    assert any(
        event.kind is ExtensionEventKind.DISPATCH_STARTED and event.hook is Hook.SESSION_RESUMED
        for event in resumed.drain_extension_events()
    )


class _AsyncRegisterExtension:
    name = "async-register"

    async def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, lambda _: Observe())


class _AsyncHookExtension:
    name = "async-hook"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, cast(Any, self._observe))

    async def _observe(self, hook_input: InputHookInput) -> Observe:
        return Observe()


class _AsyncValidatorExtension:
    name = "async-validator"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_session_entry_type("async-audit", cast(Any, self._validate))

    async def _validate(self, payload: Mapping[str, object]) -> None:
        raise ValueError("must execute synchronously")


class _NonNoneRegisterExtension:
    name = "non-none-register"

    def register(self, registry: ExtensionRegistry) -> Observe:
        return Observe()


@pytest.mark.parametrize(
    ("extension", "message"),
    (
        (_AsyncRegisterExtension(), "register must be synchronous"),
        (_AsyncHookExtension(), "hook handler must be synchronous"),
        (_NonNoneRegisterExtension(), "register must return None"),
    ),
)
def test_async_or_value_returning_registration_is_rejected_without_side_effects(
    extension: object,
    message: str,
    recwarn: pytest.WarningsRecorder,
) -> None:
    with pytest.raises(ExtensionRegistrationError, match=message):
        AgentKernel(FakeProvider(((ProviderDone(),),)), extensions=(extension,))  # type: ignore[arg-type]

    assert not [warning for warning in recwarn if warning.category is RuntimeWarning]


def test_async_session_validator_is_rejected_before_session_persistence(
    recwarn: pytest.WarningsRecorder,
) -> None:
    store = InMemorySessionStore()

    with pytest.raises(ExtensionRegistrationError, match="validator must be synchronous"):
        AgentKernel.with_new_session(
            FakeProvider(((ProviderDone(),),)),
            store,
            session_id="async-validator",
            configuration={"provider": "fake"},
            extensions=(_AsyncValidatorExtension(),),
        )

    assert store.load("async-validator") == ()
    assert not [warning for warning in recwarn if warning.category is RuntimeWarning]


class _AwaitableReturningHandler:
    def __call__(self, hook_input: InputHookInput) -> object:
        async def outcome() -> Observe:
            return Observe()

        return outcome()


class _HiddenAwaitableHookExtension:
    name = "hidden-awaitable-hook"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, cast(Any, _AwaitableReturningHandler()))


class _AwaitableReturningValidator:
    def __call__(self, payload: Mapping[str, object]) -> object:
        async def validate() -> None:
            raise ValueError("must not be deferred")

        return validate()


class _HiddenAwaitableValidatorExtension:
    name = "hidden-awaitable-validator"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_session_entry_type(
            "hidden-audit",
            cast(Any, _AwaitableReturningValidator()),
        )


def test_unexpected_awaitables_are_closed_and_rejected_before_state_mutation(
    recwarn: pytest.WarningsRecorder,
) -> None:
    store = InMemorySessionStore()
    ids = iter(("root", "must-not-persist"))
    kernel = AgentKernel.with_new_session(
        FakeProvider(((ProviderDone(),),)),
        store,
        session_id="hidden-awaitables",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
        extensions=(_HiddenAwaitableHookExtension(), _HiddenAwaitableValidatorExtension()),
    )
    branch_before = kernel.session_active_branch

    async def run_once() -> str | None:
        run = kernel.create_run("must not persist")
        async for _ in run:
            pass
        result = await run.result()
        return None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == "extension_input_rejected"
    with pytest.raises(ValueError, match="must return None synchronously"):
        kernel.append_extension_entry("hidden-audit", {"note": "must not persist"})
    assert kernel.session_active_branch == branch_before
    assert len(store.load("hidden-awaitables")) == 1
    assert not [warning for warning in recwarn if warning.category is RuntimeWarning]


class _MutatingRetryProvider:
    def __init__(self) -> None:
        self.received_tool_names: list[str] = []
        self.attempts = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.attempts += 1
        self.received_tool_names.append(str(request.tools[0]["name"]))
        request.tools[0]["name"] = "provider-mutated"
        if self.attempts == 1:
            yield ProviderError("provider_unavailable", "retry")
        else:
            yield ProviderDone()


def test_provider_receives_a_deep_snapshot_for_every_retry_attempt(tmp_path: Path) -> None:
    provider = _MutatingRetryProvider()
    kernel = AgentKernel(
        provider,
        retry_policy=RetryPolicy(max_attempts=2),
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("provider cannot mutate retries")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.SETTLED
    assert len(provider.received_tool_names) == 2
    assert provider.received_tool_names[0] == provider.received_tool_names[1]
    assert provider.received_tool_names[0] != "provider-mutated"


class _SelfCancellingProvider:
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.Event().wait()
        yield ProviderDone()  # pragma: no cover - self-cancellation must win


class _CancellingInputExtension:
    name = "cancelling-input"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._cancel)

    def _cancel(self, hook_input: InputHookInput) -> Observe:
        raise asyncio.CancelledError("extension handler requested cancellation")


def test_provider_and_hook_cancellation_do_not_impersonate_host_cancellation() -> None:
    async def run_once(kernel: AgentKernel) -> tuple[AgentRunState, str | None]:
        run = kernel.create_run("cancellation provenance")
        async for _ in run:
            pass
        result = await run.result()
        return result.state, None if result.error is None else result.error.code

    provider_result = asyncio.run(run_once(AgentKernel(_SelfCancellingProvider())))
    hook_result = asyncio.run(
        run_once(
            AgentKernel(
                FakeProvider(((ProviderDone(),),)),
                extensions=(_CancellingInputExtension(),),
            )
        )
    )

    assert provider_result == (AgentRunState.FAILED, "provider_exception")
    assert hook_result == (AgentRunState.FAILED, "extension_input_rejected")


class _SelfCancellingTool:
    spec = ToolSpec(
        name="self_cancelling_tool",
        description="Attempt to cancel the owning task.",
        schema={"type": "object", "required": [], "properties": {}, "additionalProperties": False},
        mode="sequential",
        enabled_by_default=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Callable[[str, str], object] | None,
    ) -> ToolOutput:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.Event().wait()
        return ToolOutput({})  # pragma: no cover - self-cancellation must win


def test_extension_tool_self_cancellation_is_an_aligned_tool_error(tmp_path: Path) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    runtime.register_many((_SelfCancellingTool(),), enable=True)

    async def execute() -> tuple[ToolResult, ...]:
        batch = await runtime.execute_batch((ToolCall("cancel-1", "self_cancelling_tool", {}),))
        return batch.results

    results = asyncio.run(execute())
    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].error is not None
    assert results[0].error.code == "tool_cancelled_by_extension"


class _InvalidJsonToolResultExtension:
    name = "invalid-json-tool-result"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_RESULT, self._supplement)

    def _supplement(self, hook_input: ToolResultHookInput) -> Supplement[ToolResultSupplement]:
        return Supplement(ToolResultSupplement({"nested": {1: float("nan")}}))


def test_tool_result_hook_cannot_reintroduce_non_standard_json(tmp_path: Path) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("extension_echo", {"value": "strict json"}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_ToolExtension(), _InvalidJsonToolResultExtension()),
    )

    async def run_once() -> None:
        run = kernel.create_run("reject non-standard json")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert message.results[0].status == "error"
    assert message.results[0].error is not None
    assert message.results[0].error.code == "extension_rejected"


class _NonFiniteSchemaTool:
    def __init__(self, minimum: float) -> None:
        self.spec = ToolSpec(
            name="non_finite_schema_tool",
            description="Invalid numeric schema constraint.",
            schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "number", "minimum": minimum}},
                "additionalProperties": False,
            },
            mode="parallel",
            enabled_by_default=False,
        )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Callable[[str, str], object] | None,
    ) -> ToolOutput:
        return ToolOutput({"value": arguments["value"]})


@pytest.mark.parametrize("minimum", (float("nan"), float("inf"), float("-inf")))
def test_tool_schema_rejects_non_finite_numeric_constraints(
    tmp_path: Path,
    minimum: float,
) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))

    with pytest.raises(ValueError, match="invalid minimum"):
        runtime.register(_NonFiniteSchemaTool(minimum))


class _CapturedAwaitableExtension:
    def __init__(self, name: str, awaitable: object, capability: str) -> None:
        self.name = name
        self._awaitable = awaitable
        self._capability = capability

    def register(self, registry: ExtensionRegistry) -> object:
        if self._capability == "hook":
            registry.register_hook(Hook.INPUT, cast(Any, self._handler))
            return None
        if self._capability == "validator":
            registry.register_session_entry_type("captured-task", cast(Any, self._validator))
            return None
        return self._awaitable

    def _handler(self, hook_input: InputHookInput) -> object:
        return self._awaitable

    def _validator(self, payload: Mapping[str, object]) -> object:
        return self._awaitable


@pytest.mark.parametrize("capability", ("register", "hook", "validator"))
def test_rejected_borrowed_task_results_are_not_cancelled(
    capability: str,
    recwarn: pytest.WarningsRecorder,
) -> None:
    async def scenario() -> tuple[bool, bool, int]:
        side_effects: list[str] = []

        async def delayed_side_effect() -> None:
            await asyncio.Event().wait()
            side_effects.append("ran")

        task = asyncio.create_task(delayed_side_effect())
        extension = _CapturedAwaitableExtension(
            f"captured-{capability}",
            task,
            capability,
        )
        store = InMemorySessionStore()
        if capability == "register":
            with pytest.raises(ExtensionRegistrationError, match="synchronous"):
                AgentKernel(
                    FakeProvider(((ProviderDone(),),)),
                    extensions=(cast(Any, extension),),
                )
            persisted = 0
        else:
            kernel = AgentKernel.with_new_session(
                FakeProvider(((ProviderDone(),),)),
                store,
                session_id=f"captured-{capability}",
                configuration={"provider": "fake"},
                extensions=(cast(Any, extension),),
            )
            if capability == "hook":
                run = kernel.create_run("reject captured Task")
                async for _ in run:
                    pass
                assert (await run.result()).state is AgentRunState.FAILED
            else:
                with pytest.raises(ValueError, match="must return None synchronously"):
                    kernel.append_extension_entry("captured-task", {"note": "reject"})
            persisted = len(store.load(f"captured-{capability}"))
        await asyncio.sleep(0)
        rejected_task_cancelled = task.cancelled()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rejected_task_cancelled, bool(side_effects), persisted

    cancelled, side_effect_ran, persisted = asyncio.run(scenario())
    assert cancelled is False
    assert side_effect_ran is False
    assert persisted == (0 if capability == "register" else 1)
    assert not [warning for warning in recwarn if warning.category is RuntimeWarning]


class _CompletedFutureRegisterExtension:
    name = "completed-future-register"

    def __init__(self, future: asyncio.Future[object]) -> None:
        self._future = future

    def register(self, registry: ExtensionRegistry) -> object:
        return self._future


@pytest.mark.parametrize("failed", (False, True))
def test_completed_future_from_closed_loop_is_consumed_without_mutation(failed: bool) -> None:
    loop = asyncio.new_event_loop()
    future: asyncio.Future[object] = loop.create_future()
    if failed:
        future.set_exception(ValueError("completed failure"))
    else:
        future.set_result("completed success")
    loop.close()

    with pytest.raises(ExtensionRegistrationError, match="synchronous"):
        AgentKernel(
            FakeProvider(((ProviderDone(),),)),
            extensions=(cast(Any, _CompletedFutureRegisterExtension(future)),),
        )

    assert future.done()
    assert not future.cancelled()


class _BorrowedCustomAwaitable:
    def __init__(self) -> None:
        self.cancel_called = False
        self.close_called = False

    def __await__(self) -> object:
        async def result() -> Observe:
            return Observe()

        return result().__await__()

    def cancel(self) -> None:
        self.cancel_called = True

    def close(self) -> None:
        self.close_called = True


def test_rejected_borrowed_custom_awaitable_is_not_mutated() -> None:
    awaitable = _BorrowedCustomAwaitable()
    extension = _CapturedAwaitableExtension("custom-awaitable", awaitable, "hook")
    kernel = AgentKernel(
        FakeProvider(((ProviderDone(),),)),
        extensions=(cast(Any, extension),),
    )

    async def run_once() -> AgentRunState:
        run = kernel.create_run("reject borrowed custom awaitable")
        async for _ in run:
            pass
        return (await run.result()).state

    assert asyncio.run(run_once()) is AgentRunState.FAILED
    assert awaitable.cancel_called is False
    assert awaitable.close_called is False


class _CoroutineStyleProvider:
    async def stream(self, request: ProviderRequest) -> None:
        return None


class _CoroutineProviderExtension:
    name = "coroutine-provider-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_provider("coroutine-provider", cast(Any, _CoroutineStyleProvider()))


def test_coroutine_style_provider_is_rejected_or_disposed_without_warning(
    recwarn: pytest.WarningsRecorder,
) -> None:
    with pytest.raises(ExtensionRegistrationError, match="async iterator"):
        AgentKernel(
            "coroutine-provider",
            extensions=(_CoroutineProviderExtension(),),
        )

    async def direct_run() -> tuple[AgentRunState, str | None]:
        kernel = AgentKernel(cast(Any, _CoroutineStyleProvider()))
        run = kernel.create_run("reject coroutine Provider")
        async for _ in run:
            pass
        result = await run.result()
        return result.state, None if result.error is None else result.error.code

    assert asyncio.run(direct_run()) == (AgentRunState.FAILED, "provider_exception")
    assert not [warning for warning in recwarn if warning.category is RuntimeWarning]


class _ReturningSelfCancellingInputExtension:
    name = "returning-self-cancelling-input"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._cancel)

    def _cancel(self, hook_input: InputHookInput) -> Observe:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return Observe()


class _SelfCancellingFactoryProvider:
    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return FakeProvider(((ProviderDone(),),)).stream(request)


class _RaisingCancelledFactoryProvider:
    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        raise asyncio.CancelledError("Provider factory cancellation")


class _LoopAwareFactoryProvider:
    def __init__(self) -> None:
        self.factory_loop: asyncio.AbstractEventLoop | None = None

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        self.factory_loop = asyncio.get_running_loop()

        async def events() -> AsyncIterator[ProviderStreamEvent]:
            yield ProviderDone()

        return events()


def test_sync_hook_and_provider_factory_cannot_cancel_the_authoritative_task() -> None:
    async def run_once(kernel: AgentKernel) -> tuple[AgentRunState, str | None]:
        run = kernel.create_run("isolate sync cancellation")
        async for _ in run:
            pass
        result = await run.result()
        return result.state, None if result.error is None else result.error.code

    hook = asyncio.run(
        run_once(
            AgentKernel(
                FakeProvider(((ProviderDone(),),)),
                extensions=(_ReturningSelfCancellingInputExtension(),),
            )
        )
    )
    self_cancel_factory = asyncio.run(run_once(AgentKernel(_SelfCancellingFactoryProvider())))
    raising_factory = asyncio.run(run_once(AgentKernel(_RaisingCancelledFactoryProvider())))

    assert hook == (AgentRunState.FAILED, "extension_input_rejected")
    assert self_cancel_factory == (AgentRunState.FAILED, "provider_exception")
    assert raising_factory == (AgentRunState.FAILED, "provider_exception")


def test_provider_factory_keeps_running_loop_semantics_inside_isolated_child_task() -> None:
    provider = _LoopAwareFactoryProvider()

    async def run_once() -> tuple[AgentRunState, bool]:
        authoritative_loop = asyncio.get_running_loop()
        run = AgentKernel(provider).create_run("loop-aware factory")
        async for _ in run:
            pass
        result = await run.result()
        return result.state, provider.factory_loop is authoritative_loop

    assert asyncio.run(run_once()) == (AgentRunState.SETTLED, True)


class _CancellingCloseIterator:
    def __init__(self, event: ProviderStreamEvent) -> None:
        self._event = event
        self._emitted = False

    def __aiter__(self) -> _CancellingCloseIterator:
        return self

    async def __anext__(self) -> ProviderStreamEvent:
        if self._emitted:
            raise StopAsyncIteration
        self._emitted = True
        return self._event

    async def aclose(self) -> None:
        raise asyncio.CancelledError("Provider cleanup cancellation")


class _CancellingCloseProvider:
    def __init__(self, event: ProviderStreamEvent) -> None:
        self._event = event

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        return _CancellingCloseIterator(self._event)


@pytest.mark.parametrize(
    ("event", "expected_code"),
    (
        (ProviderDone(), "provider_exception"),
        (ProviderError("provider_fatal", "primary failure"), "provider_fatal"),
    ),
)
def test_provider_cleanup_cancellation_never_impersonates_host_cancellation(
    event: ProviderStreamEvent,
    expected_code: str,
) -> None:
    kernel = AgentKernel(_CancellingCloseProvider(event))

    async def run_once() -> tuple[AgentRunState, str | None]:
        run = kernel.create_run("cleanup cancellation")
        async for _ in run:
            pass
        result = await run.result()
        return result.state, None if result.error is None else result.error.code

    assert asyncio.run(run_once()) == (AgentRunState.FAILED, expected_code)
    assert any(
        extension_event.kind is ExtensionEventKind.RUNTIME_FAILURE
        and extension_event.code == "provider_cleanup_failed"
        for extension_event in kernel.drain_extension_events()
    )


def test_raw_non_standard_tool_call_json_is_provider_owned_and_never_published() -> None:
    provider = FakeProvider(
        (
            (
                ProviderToolCallStart(0),
                ProviderToolCallDelta(
                    0,
                    call_id_delta="raw-nan",
                    tool_name_delta="bash",
                    arguments_delta='{"command":"echo safe","timeout":NaN}',
                ),
                ProviderToolCallEnd(0),
                ProviderDone("tool_use"),
            ),
        )
    )
    kernel = AgentKernel(provider)

    async def run_once() -> tuple[str | None, str | None, list[AgentSessionEvent]]:
        run = kernel.create_run("reject raw NaN")
        events = [event async for event in run]
        result = await run.result()
        if result.error is None:
            return None, None, events
        return result.error.code, result.error.source, events

    code, source, events = asyncio.run(run_once())
    assert (code, source) == ("provider_exception", "provider")
    assert all(
        event.message is None or not event.message.tool_calls
        for event in events
        if event.kind is AgentSessionEventKind.MESSAGE_UPDATE
    )


class _NonFiniteToolCallTransformExtension:
    name = "non-finite-tool-call-transform"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._transform)

    def _transform(self, hook_input: ToolCallHookInput) -> Transform[ToolCall]:
        arguments = hook_input.arguments
        arguments["timeout"] = float("nan")
        return Transform(ToolCall(hook_input.call_id, hook_input.tool_name, arguments))


def test_tool_call_transform_rejects_non_finite_arguments_before_execution(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        (
            _tool_call_events("bash", {"command": "echo safe", "timeout": 1}),
            (ProviderDone(),),
        )
    )
    kernel = AgentKernel(
        provider,
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_NonFiniteToolCallTransformExtension(),),
    )

    async def run_once() -> None:
        run = kernel.create_run("reject transformed NaN")
        async for _ in run:
            pass
        await run.result()

    asyncio.run(run_once())
    message = provider.requests[1].messages[-1]
    assert isinstance(message, ToolResultMessage)
    assert message.results[0].status == "error"
    assert message.results[0].error is not None
    assert message.results[0].error.code == "extension_rejected"
    events = kernel.drain_extension_events()
    assert any(
        event.kind is ExtensionEventKind.OUTCOME_REJECTED
        and event.hook is Hook.TOOL_CALL
        and event.extension_name == "non-finite-tool-call-transform"
        for event in events
    )
    assert not any(
        event.kind is ExtensionEventKind.HANDLER_OUTCOME
        and event.hook is Hook.TOOL_CALL
        and event.extension_name == "non-finite-tool-call-transform"
        and event.revalidated
        for event in events
    )


def test_huge_integer_schema_constraint_fails_deterministically_without_overflow(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
    before = runtime.registered_names

    with pytest.raises(ValueError, match="Tool schema"):
        runtime.register(_NonFiniteSchemaTool(10**10_000))

    assert runtime.registered_names == before
