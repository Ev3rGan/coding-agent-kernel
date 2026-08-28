"""Explicit Extension examples that use only production public seams."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Literal

from coding_agent.context import ContextHookInput
from coding_agent.environment import LocalCodingEnvironment
from coding_agent.events import (
    AgentRunResult,
    AgentSessionEvent,
    ProviderDone,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
)
from coding_agent.extensions import (
    AgentSettledHookInput,
    Block,
    ContextSupplement,
    Extension,
    ExtensionEvent,
    ExtensionRegistry,
    Hook,
    InputHookInput,
    Observe,
    SessionEntryDraft,
    Supplement,
    ToolCallHookInput,
    Transform,
)
from coding_agent.kernel import AgentKernel
from coding_agent.provider import FakeProvider, ProviderRequest
from coding_agent.session import JsonlSessionStore, SessionEntry
from coding_agent.tool_runtime import ToolRuntime
from coding_agent.tools import ToolOutput, ToolProgressCallback, ToolSpec

ExtensionDemoCase = Literal["success", "ordering", "invalid-mutation"]


@dataclass(frozen=True, slots=True)
class ExtensionDemoReport:
    case: ExtensionDemoCase
    events: tuple[AgentSessionEvent, ...]
    extension_events: tuple[ExtensionEvent, ...]
    result: AgentRunResult
    provider_requests: tuple[ProviderRequest, ...]
    session_entries: tuple[SessionEntry, ...]
    jsonl_path: Path


class ExtensionEchoTool:
    """Small custom Tool that still executes through the real ToolRuntime."""

    spec = ToolSpec(
        name="extension_echo",
        description="Echo a string through an explicitly registered Extension Tool.",
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
        on_progress: ToolProgressCallback | None,
    ) -> ToolOutput:
        del environment, cancel_event
        if on_progress is not None:
            await on_progress("extension", "echo")
        return ToolOutput({"echo": arguments["value"]})


def _validate_audit(payload: Mapping[str, object]) -> None:
    if not isinstance(payload.get("note"), str):
        raise ValueError("extension_audit.note must be a string")


class ExampleExtension:
    """Register one Tool, Context supplement, ToolCall block, and custom entry."""

    name = "example-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_tool(ExtensionEchoTool())
        registry.register_session_entry_type("extension_audit", _validate_audit)
        registry.register_hook(Hook.CONTEXT, self._context)
        registry.register_hook(Hook.TOOL_CALL, self._tool_call)
        registry.register_hook(Hook.AGENT_SETTLED, self._settled)

    def _context(self, hook_input: ContextHookInput) -> Supplement[ContextSupplement]:
        del hook_input
        return Supplement(ContextSupplement(project_context=("EXAMPLE_EXTENSION_CONTEXT",)))

    def _tool_call(self, hook_input: ToolCallHookInput) -> Observe | Block:
        if hook_input.arguments.get("value") == "blocked":
            return Block("example_block", "the example blocks this ToolCall")
        return Observe()

    def _settled(self, hook_input: AgentSettledHookInput) -> Supplement[SessionEntryDraft]:
        return Supplement(
            SessionEntryDraft(
                "extension_audit",
                {"note": f"terminal:{hook_input.result.state.value}"},
            )
        )


class OrderingExtension:
    """One of two explicitly ordered transform/supplement participants."""

    def __init__(self, name: str, marker: str) -> None:
        self.name = name
        self._marker = marker

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._input)
        registry.register_hook(Hook.CONTEXT, self._context)

    def _input(self, hook_input: InputHookInput) -> Transform[str]:
        return Transform(f"{hook_input.prompt}|{self._marker}")

    def _context(self, hook_input: ContextHookInput) -> Supplement[ContextSupplement]:
        del hook_input
        return Supplement(ContextSupplement(project_context=(self._marker,)))


class InvalidMutationExtension:
    """Demonstrate that an invalid handler cannot escape into Kernel state."""

    name = "invalid-mutation-extension"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.INPUT, self._invalid)

    def _invalid(self, hook_input: InputHookInput) -> Observe:
        del hook_input
        raise RuntimeError("attempted to mutate Kernel-owned input state")


def _tool_call_events(
    index: int,
    call_id: str,
    arguments: dict[str, object],
) -> tuple[ProviderStreamEvent, ...]:
    return (
        ProviderToolCallStart(index),
        ProviderToolCallDelta(
            index,
            call_id_delta=call_id,
            tool_name_delta="extension_echo",
            arguments_delta=json.dumps(arguments, separators=(",", ":")),
        ),
        ProviderToolCallEnd(index),
    )


async def run_extension_demo(case: ExtensionDemoCase) -> ExtensionDemoReport:
    """Run one deterministic Extension scenario through AgentKernel/AgentRun."""

    workspace = Path(tempfile.mkdtemp(prefix=f"coding-agent-extensions-{case}-"))
    store = JsonlSessionStore(workspace / "session.jsonl")
    identifiers = count(1)

    def entry_id_factory() -> str:
        return f"entry-{next(identifiers):04d}"

    if case == "success":
        provider = FakeProvider(
            (
                (
                    *_tool_call_events(0, "allowed", {"value": "allowed"}),
                    *_tool_call_events(1, "blocked", {"value": "blocked"}),
                    ProviderDone("tool_use"),
                ),
                (ProviderTextDelta("Extension scenario complete."), ProviderDone()),
            )
        )
        runtime = ToolRuntime(LocalCodingEnvironment(workspace))
        extensions: tuple[Extension, ...] = (ExampleExtension(),)
    elif case == "ordering":
        provider = FakeProvider(((ProviderTextDelta("Ordering complete."), ProviderDone()),))
        runtime = None
        extensions = (
            OrderingExtension("ordering-one", "ONE"),
            OrderingExtension("ordering-two", "TWO"),
        )
    else:
        provider = FakeProvider(((ProviderDone(),),))
        runtime = None
        extensions = (InvalidMutationExtension(),)

    kernel = AgentKernel.with_new_session(
        provider,
        store,
        session_id=f"extensions-{case}",
        configuration={"provider": "fake", "demo": "extensions", "case": case},
        entry_id_factory=entry_id_factory,
        tool_runtime=runtime,
        extensions=extensions,
    )
    prompt = "ordering" if case == "ordering" else "run extension demonstration"
    run = kernel.create_run(prompt)
    events = tuple([event async for event in run])
    result = await run.result()
    return ExtensionDemoReport(
        case=case,
        events=events,
        extension_events=kernel.drain_extension_events(),
        result=result,
        provider_requests=tuple(provider.requests),
        session_entries=kernel.session_active_branch,
        jsonl_path=store.path,
    )
