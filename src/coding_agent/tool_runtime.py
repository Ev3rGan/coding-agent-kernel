"""Core ToolRuntime registration, validation, scheduling, and normalization."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from coding_agent.environment import CommandTimeoutError, LocalCodingEnvironment
from coding_agent.events import ToolCall, ToolError, ToolProgress, ToolResult
from coding_agent.tools import Tool, ToolExecutionError, ToolSpec, builtin_tools


@dataclass(frozen=True, slots=True)
class ToolBatchResult:
    mode: Literal["parallel", "sequential"]
    results: tuple[ToolResult, ...]
    completion_order: tuple[str, ...]


class InvalidArgumentsError(ValueError):
    pass


ToolProgressCallback = Callable[[ToolProgress], Awaitable[None]]


class ToolRuntime:
    """Own tool discovery, enabled state, deterministic batches, and result shape."""

    def __init__(
        self,
        environment: LocalCodingEnvironment,
        *,
        enabled: set[str] | None = None,
    ) -> None:
        self.environment = environment
        self._tools: dict[str, Tool] = {}
        for tool in builtin_tools():
            self.register(tool)
        defaults = {name for name, tool in self._tools.items() if tool.spec.enabled_by_default}
        self._enabled = defaults if enabled is None else set(enabled)

    @property
    def registered_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    @property
    def enabled_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._enabled))

    @property
    def schemas(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name].spec for name in sorted(self._enabled))

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def enable(self, *names: str) -> None:
        unknown = set(names) - self._tools.keys()
        if unknown:
            raise KeyError(f"unknown tools: {sorted(unknown)}")
        self._enabled.update(names)

    async def execute_batch(
        self,
        calls: tuple[ToolCall, ...],
        cancel_event: asyncio.Event | None = None,
        *,
        on_progress: ToolProgressCallback | None = None,
    ) -> ToolBatchResult:
        mode = self.batch_mode(calls)
        completion: list[str] = []
        indexed: list[tuple[int, ToolResult]] = []

        async def execute(index: int, call: ToolCall) -> tuple[int, ToolResult]:
            result = await self._execute_one(call, cancel_event, on_progress)
            return index, result

        if mode == "sequential":
            for index, call in enumerate(calls):
                item = await execute(index, call)
                indexed.append(item)
                completion.append(call.call_id)
        else:
            tasks = [asyncio.create_task(execute(index, call)) for index, call in enumerate(calls)]
            try:
                for completed in asyncio.as_completed(tasks):
                    index, result = await completed
                    indexed.append((index, result))
                    completion.append(result.call_id)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        ordered = tuple(result for _, result in sorted(indexed, key=lambda item: item[0]))
        return ToolBatchResult(mode, ordered, tuple(completion))

    def batch_mode(self, calls: tuple[ToolCall, ...]) -> Literal["parallel", "sequential"]:
        """Return the one authoritative scheduling mode for a batch."""

        return (
            "sequential" if any(self._mode(call) == "sequential" for call in calls) else "parallel"
        )

    def _mode(self, call: ToolCall) -> Literal["parallel", "sequential"]:
        tool = self._tools.get(call.tool_name)
        return "parallel" if tool is None else tool.spec.mode

    async def _execute_one(
        self,
        call: ToolCall,
        cancel_event: asyncio.Event | None,
        on_progress: ToolProgressCallback | None,
    ) -> ToolResult:
        tool = self._tools.get(call.tool_name)
        if tool is None:
            return self._error(call, "unknown_tool", f"unknown tool: {call.tool_name}")
        if call.tool_name not in self._enabled:
            return self._error(call, "tool_disabled", f"tool is not enabled: {call.tool_name}")
        if cancel_event is not None and cancel_event.is_set():
            return ToolResult(
                call.call_id,
                call.tool_name,
                "cancelled",
                error=ToolError("cancelled", "tool execution was cancelled"),
            )
        try:
            self._validate(call.arguments, tool.spec.schema)

            async def report(stream: str, data: str) -> None:
                if on_progress is not None:
                    await on_progress(ToolProgress(call.call_id, call.tool_name, stream, data))

            output = await tool.execute(call.arguments, self.environment, cancel_event, report)
            return ToolResult(call.call_id, call.tool_name, "success", output.data)
        except InvalidArgumentsError as exc:
            return self._error(call, "invalid_arguments", str(exc))
        except CommandTimeoutError as exc:
            return self._error(call, "timeout", str(exc))
        except ToolExecutionError as exc:
            return self._error(call, exc.code, str(exc), exc.output)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return ToolResult(
                call.call_id,
                call.tool_name,
                "cancelled",
                error=ToolError("cancelled", "tool execution was cancelled"),
            )
        except (OSError, ValueError, re.error) as exc:
            return self._error(call, "tool_error", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _error(
        call: ToolCall, code: str, message: str, output: dict[str, Any] | None = None
    ) -> ToolResult:
        return ToolResult(call.call_id, call.tool_name, "error", output, ToolError(code, message))

    @staticmethod
    def _validate(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
        properties: dict[str, dict[str, Any]] = schema["properties"]
        missing = [name for name in schema["required"] if name not in arguments]
        extra = set(arguments) - properties.keys()
        if missing:
            raise InvalidArgumentsError(f"missing required arguments: {', '.join(missing)}")
        if extra:
            raise InvalidArgumentsError(f"unexpected arguments: {', '.join(sorted(extra))}")
        expected_types: dict[str, type[object] | tuple[type[object], ...]] = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
        }
        for name, value in arguments.items():
            expected = properties[name].get("type")
            if expected in {"number", "integer"} and isinstance(value, bool):
                raise InvalidArgumentsError(f"argument {name!r} must be {expected}")
            if expected in expected_types and not isinstance(value, expected_types[expected]):
                raise InvalidArgumentsError(f"argument {name!r} must be {expected}")
            minimum = properties[name].get("minimum")
            if minimum is not None and value < minimum:
                raise InvalidArgumentsError(f"argument {name!r} must be at least {minimum}")
