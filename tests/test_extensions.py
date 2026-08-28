from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coding_agent import (
    AgentKernel,
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    ContextSupplement,
    ExtensionRuntime,
    FakeProvider,
    HookInput,
    HookName,
    HookResult,
    LocalCodingEnvironment,
    ProviderDone,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ToolCall,
    ToolResultSupplement,
    ToolRuntime,
)
from coding_agent.extensions_example import SampleExtension, ShoutTool


class _TransformingExtension:
    """A Tool Handler that rewrites a ToolCall's arguments before execution."""

    @property
    def name(self) -> str:
        return "rewriter"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.TOOL_CALL, self._rewrite)

    def _rewrite(self, input_: HookInput) -> HookResult:
        call = input_.tool_call
        if call is not None and call.tool_name == "read":
            rewritten = ToolCall(call.call_id, "shout", {"text": "rewritten"})
            return HookResult.transform_tool_call(rewritten)
        return HookResult.observe()


class _BlockingExtension:
    """A Tool Handler that blocks one deterministic ToolCall id."""

    @property
    def name(self) -> str:
        return "blocker"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.TOOL_CALL, self._block)

    def _block(self, input_: HookInput) -> HookResult:
        call = input_.tool_call
        if call is not None and call.call_id == "forbidden-1":
            return HookResult.block("extension policy says no")
        return HookResult.observe()


class _BrokenExtension:
    """An Extension whose handler returns an illegal mutation."""

    @property
    def name(self) -> str:
        return "broken"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.CONTEXT, self._context)

    def _context(self, input_: HookInput) -> HookResult:
        del input_
        return HookResult.supplement_tool_result(ToolResultSupplement("bad"))


def _tool_call_script(
    call_id: str, name: str, arguments: dict[str, object]
) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCallStart(index=0),
        ProviderToolCallDelta(
            index=0,
            call_id_delta=call_id,
            tool_name_delta=name,
            arguments_delta=json.dumps(arguments),
        ),
        ProviderToolCallEnd(index=0),
        ProviderDone(stop_reason="tool_use"),
    )


async def _collect(run: object) -> list[AgentSessionEvent]:
    events: list[AgentSessionEvent] = []
    async for event in run:  # type: ignore[attr-defined]
        events.append(event)
    return events


def test_context_supplement_lines_merge_in_registration_order() -> None:
    async def scenario() -> None:
        provider = FakeProvider(((ProviderTextDelta("done"), ProviderDone()),))
        kernel = AgentKernel(provider, extensions=[SampleExtension(), _Second()])
        run = kernel.create_run("context supplement")
        await _collect(run)
        request = provider.requests[0]
        assert list(request.project_context) == ["EXTENSION_RESOURCE", "SECOND_RESOURCE"]

    asyncio.run(scenario())


def test_tool_call_transform_revalidated_and_executed(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        runtime.register(ShoutTool())
        runtime.enable("shout")
        provider = FakeProvider(
            [
                _tool_call_script("rewrite-1", "read", {"path": "x.py"}),
                (ProviderTextDelta("rewrote"), ProviderDone()),
            ]
        )
        kernel = AgentKernel(
            provider,
            tool_runtime=runtime,
            extensions=[_TransformingExtension()],
        )
        run = kernel.create_run("transform")
        events = await _collect(run)
        result = await run.result()
        assert result.state is AgentRunState.SETTLED
        tool_results = [
            event.tool_result
            for event in events
            if event.kind is AgentSessionEventKind.TOOL_EXECUTION_END
            and event.tool_result is not None
        ]
        assert len(tool_results) == 1
        assert tool_results[0].tool_name == "shout"
        assert tool_results[0].status == "success"
        assert tool_results[0].output == {"content": "REWRITTEN"}

    asyncio.run(scenario())


def test_tool_call_block_produces_error_result_without_executing(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        provider = FakeProvider(
            [
                _tool_call_script("forbidden-1", "read", {"path": "x.py"}),
                (ProviderTextDelta("blocked handled"), ProviderDone()),
            ]
        )
        kernel = AgentKernel(
            provider,
            tool_runtime=runtime,
            extensions=[_BlockingExtension()],
        )
        run = kernel.create_run("block")
        events = await _collect(run)
        result = await run.result()
        assert result.state is AgentRunState.SETTLED
        tool_results = [
            event.tool_result
            for event in events
            if event.kind is AgentSessionEventKind.TOOL_EXECUTION_END
            and event.tool_result is not None
        ]
        assert len(tool_results) == 1
        assert tool_results[0].status == "error"
        assert tool_results[0].error is not None
        assert tool_results[0].error.code == "extension_blocked"
        assert tool_results[0].error.message == "extension policy says no"
        blocked_events = [
            event.kind.value == "hook_blocked"
            and event.extension == "blocker"
            for event in kernel.extension_events
        ]
        assert any(blocked_events)

    asyncio.run(scenario())


def test_illegal_mutation_is_rejected_and_run_survives() -> None:
    async def scenario() -> None:
        provider = FakeProvider(((ProviderTextDelta("ok"), ProviderDone()),))
        kernel = AgentKernel(provider, extensions=[_BrokenExtension()])
        run = kernel.create_run("broken")
        await _collect(run)
        result = await run.result()
        assert result.state is AgentRunState.SETTLED
        error_events = [
            event
            for event in kernel.extension_events
            if event.kind.value == "hook_error"
        ]
        assert len(error_events) == 1
        assert "does not match hook" in error_events[0].message
        assert all(
            event is not None
            and event.extension == "broken"
            for event in [item for item in error_events if item]
        )

    asyncio.run(scenario())


def test_extension_events_are_separate_from_public_session_stream() -> None:
    async def scenario() -> None:
        provider = FakeProvider(((ProviderTextDelta("hi"), ProviderDone()),))
        kernel = AgentKernel(provider, extensions=[SampleExtension()])
        run = kernel.create_run("separation")
        events = await _collect(run)
        session_kinds = {event.kind.value for event in events}
        assert all(kind not in session_kinds for kind in ("hook_start", "registered"))
        assert any(event.extension == "sample" for event in kernel.extension_events)

    asyncio.run(scenario())


def test_declared_tool_is_registered_and_session_entry_kind_declared() -> None:
    async def scenario() -> None:
        provider = FakeProvider(((ProviderTextDelta("x"), ProviderDone()),))
        kernel = AgentKernel(provider, extensions=[SampleExtension()])
        await _collect(kernel.create_run("declarations"))
        assert "sample" in kernel.extension_names
        # A fresh SampleExtension, registered through a probe runtime, declares its kind.
        probe = ExtensionRuntime()
        sample = SampleExtension()
        probe.register(sample)
        assert "extension_note" in probe.session_entry_kinds

    asyncio.run(scenario())


class _Second:
    @property
    def name(self) -> str:
        return "second"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.CONTEXT, self._context)

    def _context(self, input_: HookInput) -> HookResult:
        del input_
        return HookResult.supplement_context(ContextSupplement(("SECOND_RESOURCE",)))


class _ProviderRequestBlocker:
    """An Extension that blocks every Provider request, even on the first turn."""

    @property
    def name(self) -> str:
        return "provider-blocker"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.PROVIDER_REQUEST, self._block)

    def _block(self, input_: HookInput) -> HookResult:
        del input_
        return HookResult.block("provider policy")


class _ChainObserver:
    """An Extension that records the tool_call it actually observes in the chain."""

    def __init__(self) -> None:
        self.seen: str | None = None

    @property
    def name(self) -> str:
        return "chain"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.TOOL_CALL, self._observe)

    def _observe(self, input_: HookInput) -> HookResult:
        self.seen = None if input_.tool_call is None else input_.tool_call.tool_name
        return HookResult.observe()


def test_provider_request_hook_runs_on_first_turn() -> None:
    async def scenario() -> None:
        provider = FakeProvider(((ProviderTextDelta("x"), ProviderDone()),))
        kernel = AgentKernel(provider, extensions=[_ProviderRequestBlocker()])
        run = kernel.create_run("first-turn block")
        await _collect(run)
        # 首轮 provider_request 就被拦截，未发出任何 Provider 请求。
        assert len(provider.requests) == 0
        assert any(
            event.kind.value == "hook_blocked" and event.extension == "provider-blocker"
            for event in kernel.extension_events
        )

    asyncio.run(scenario())


def test_tool_call_transform_composes_in_registration_order(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        runtime.register(ShoutTool())
        runtime.enable("shout")
        provider = FakeProvider(
            [
                _tool_call_script("chain-1", "read", {"path": "x.py"}),
                (ProviderTextDelta("done"), ProviderDone()),
            ]
        )
        observer = _ChainObserver()
        kernel = AgentKernel(
            provider,
            tool_runtime=runtime,
            extensions=[_TransformingExtension(), observer],
        )
        run = kernel.create_run("transform chain")
        await _collect(run)
        # rewriter 先把 read 改成 shout，observer 作为后续 handler 应看到改写后的结果。
        assert observer.seen == "shout"

    asyncio.run(scenario())


class _NoteExtension:
    """An Extension that attaches a model-visible note to every ToolResult."""

    @property
    def name(self) -> str:
        return "note"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.TOOL_RESULT, self._note)

    def _note(self, input_: HookInput) -> HookResult:
        if input_.tool_result is None:
            return HookResult.observe()
        return HookResult.supplement_tool_result(
            ToolResultSupplement(f"reviewed:{input_.tool_result.tool_name}")
        )


class _CallIdMutatorExtension:
    """An Extension that illegally rewrites a ToolCall's call_id."""

    @property
    def name(self) -> str:
        return "id-mutator"

    def register(self, runtime: ExtensionRuntime) -> None:
        runtime.on(self.name, HookName.TOOL_CALL, self._mutate)

    def _mutate(self, input_: HookInput) -> HookResult:
        call = input_.tool_call
        if call is not None and call.tool_name == "read":
            return HookResult.transform_tool_call(
                ToolCall("different-id", "shout", {"text": "x"})
            )
        return HookResult.observe()


def test_tool_result_supplement_becomes_model_visible_note(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        runtime.register(ShoutTool())
        runtime.enable("shout")
        provider = FakeProvider(
            [
                _tool_call_script("note-1", "shout", {"text": "hi"}),
                (ProviderTextDelta("done"), ProviderDone()),
            ]
        )
        kernel = AgentKernel(
            provider,
            tool_runtime=runtime,
            extensions=[_NoteExtension()],
        )
        run = kernel.create_run("note")
        await _collect(run)
        asserted = False
        for request in provider.requests:
            for message in request.messages:
                if getattr(message, "role", None) == "tool":
                    for result in message.results:
                        if result.note:
                            assert result.note == "reviewed:shout"
                            asserted = True
        assert asserted

    asyncio.run(scenario())


def test_tool_call_transform_may_not_change_call_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = ToolRuntime(LocalCodingEnvironment(tmp_path))
        provider = FakeProvider(
            [
                _tool_call_script("mutate-1", "read", {"path": "x.py"}),
                (ProviderTextDelta("done"), ProviderDone()),
            ]
        )
        kernel = AgentKernel(
            provider,
            tool_runtime=runtime,
            extensions=[_CallIdMutatorExtension()],
        )
        run = kernel.create_run("mutate")
        await _collect(run)
        result = await run.result()
        assert result.state is AgentRunState.SETTLED
        assert any(
            event.kind.value == "hook_error" and event.extension == "id-mutator"
            for event in kernel.extension_events
        )

    asyncio.run(scenario())