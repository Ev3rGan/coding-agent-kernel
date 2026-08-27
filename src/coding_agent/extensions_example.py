"""A concrete example Extension demonstrating the fixed Extension contract.

It registers one custom Tool, one custom SessionEntry kind, a context
supplement, a tool_call block, and an agent_settled observation. It is the
explicitly-loaded fixture the CLI demo and focused tests drive, and shows the
four allowed outcomes of the deterministic composition seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from coding_agent.extensions import (
    ContextSupplement,
    ExtensionRuntime,
    HookInput,
    HookName,
    HookResult,
)
from coding_agent.tools import ToolOutput, ToolSpec


def _shout_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    }


class ShoutTool:
    """A deterministic demo Tool that uppercases a text argument."""

    spec = ToolSpec(
        "shout",
        "Return the supplied text uppercased, as an extension-owned Tool.",
        _shout_schema(),
        "parallel",
        True,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: object,
        cancel_event: asyncio.Event | None,
        on_progress: Callable[[str, str], Awaitable[None]] | None,
    ) -> ToolOutput:
        del environment, cancel_event, on_progress
        return ToolOutput({"content": str(arguments["text"]).upper()})


class SampleExtension:
    """A fixed, explicitly loaded Extension proving extension-owned capabilities.

    attributes:
        blocked_call: the ToolCall id this Extension blocks, or None.
        observed_settled: whether the agent_settled Hook ran.
        declared_kind: the custom SessionEntry kind it declares.
    """

    def __init__(
        self,
        *,
        blocked_call: str | None = None,
        block_reason: str = "extension policy",
        context_supplement: tuple[str, ...] = ("EXTENSION_RESOURCE",),
        declared_kind: str = "extension_note",
    ) -> None:
        self.name = "sample"
        self.blocked_call = blocked_call
        self.block_reason = block_reason
        self.context_supplement = context_supplement
        self.declared_kind = declared_kind
        self.observed_settled = False

    def register(self, runtime: ExtensionRuntime) -> None:
        """Register owned capabilities through the fixed ExtensionRuntime seam."""
        runtime.declare_tool(self.name, ShoutTool(), enabled=True)
        runtime.declare_session_entry_kind(self.declared_kind)
        runtime.on(self.name, HookName.CONTEXT, self._context)
        runtime.on(self.name, HookName.TOOL_CALL, self._tool_call)
        runtime.on(self.name, HookName.AGENT_SETTLED, self._agent_settled)

    async def _context(self, input_: HookInput) -> HookResult:
        del input_
        return HookResult.supplement_context(
            ContextSupplement(self.context_supplement)
        )

    async def _tool_call(self, input_: HookInput) -> HookResult:
        call = input_.tool_call
        if call is not None and call.call_id == self.blocked_call:
            return HookResult.block(self.block_reason)
        return HookResult.observe()

    def _agent_settled(self, input_: HookInput) -> HookResult:
        del input_
        self.observed_settled = True
        return HookResult.observe()