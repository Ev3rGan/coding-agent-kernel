"""Core ToolRuntime registration, validation, scheduling, and normalization."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from coding_agent.environment import CommandTimeoutError, LocalCodingEnvironment
from coding_agent.events import ToolCall, ToolError, ToolProgress, ToolResult
from coding_agent.json_contract import json_object_snapshot
from coding_agent.tools import Tool, ToolExecutionError, ToolOutput, ToolSpec, builtin_tools


@dataclass(frozen=True, slots=True)
class ToolBatchResult:
    mode: Literal["parallel", "sequential"]
    results: tuple[ToolResult, ...]
    completion_order: tuple[str, ...]


class InvalidArgumentsError(ValueError):
    pass


ToolProgressCallback = Callable[[ToolProgress], Awaitable[None]]


class _ReadOnlyCancellation(asyncio.Event):
    """Minimal live view of Run cancellation without a writable ``set`` seam."""

    __slots__ = ("__event",)

    def __init__(self, event: asyncio.Event) -> None:
        super().__init__()
        self.__event = event

    def is_set(self) -> bool:
        return self.__event.is_set()

    async def wait(self) -> Literal[True]:
        return await self.__event.wait()

    def set(self) -> None:
        raise RuntimeError("Run cancellation is read-only inside a Tool")

    def clear(self) -> None:
        raise RuntimeError("Run cancellation is read-only inside a Tool")


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    """Kernel-owned Tool contract snapshot plus its delegated implementation."""

    implementation: Tool
    spec: ToolSpec

    async def execute(
        self,
        arguments: dict[str, Any],
        environment: LocalCodingEnvironment,
        cancel_event: asyncio.Event | None,
        on_progress: Callable[[str, str], Awaitable[None]] | None,
    ) -> ToolOutput:
        safe_arguments = json_object_snapshot(arguments, label="ToolCall arguments")
        cancellation_view = None if cancel_event is None else _ReadOnlyCancellation(cancel_event)
        execution = asyncio.create_task(
            self.implementation.execute(
                safe_arguments,
                environment,
                cancellation_view,
                on_progress,
            )
        )
        try:
            return await execution
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            if cancel_event is not None and cancel_event.is_set():
                raise
            raise _ToolOwnedCancellationError(
                "Extension Tool cancelled its own execution task"
            ) from exc
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)


class _ToolOwnedCancellationError(RuntimeError):
    pass


class ToolRuntime:
    """Own tool discovery, enabled state, deterministic batches, and result shape."""

    def __init__(
        self,
        environment: LocalCodingEnvironment,
        *,
        enabled: set[str] | None = None,
    ) -> None:
        self.environment = environment
        self._tools: dict[str, _RegisteredTool] = {}
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
        return tuple(self._snapshot_spec(self._tools[name].spec) for name in sorted(self._enabled))

    def register(self, tool: Tool) -> None:
        self.register_many((tool,))

    def register_many(self, tools: tuple[Tool, ...], *, enable: bool = False) -> None:
        """Validate a capability batch fully before changing runtime state."""

        candidates = self.validate_registration(tools)
        self._tools.update(candidates)
        if enable:
            self._enabled.update(candidates)

    def validate_registration(self, tools: tuple[Tool, ...]) -> dict[str, _RegisteredTool]:
        """Return a validated capability batch without changing runtime state."""

        candidates: dict[str, _RegisteredTool] = {}
        for tool in tools:
            spec = getattr(tool, "spec", None)
            execute = getattr(tool, "execute", None)
            if (
                not isinstance(spec, ToolSpec)
                or not callable(execute)
                or not inspect.iscoroutinefunction(execute)
            ):
                if callable(execute) and not inspect.iscoroutinefunction(execute):
                    raise TypeError("registered tools must define async execute")
                raise TypeError("registered tools must satisfy the Tool contract")
            self._validate_spec(spec)
            if spec.name in self._tools or spec.name in candidates:
                raise ValueError(f"tool already registered: {spec.name}")
            candidates[spec.name] = _RegisteredTool(tool, self._snapshot_spec(spec))
        return candidates

    @staticmethod
    def _snapshot_spec(spec: ToolSpec) -> ToolSpec:
        schema = json_object_snapshot(spec.schema, label="Tool schema")
        return ToolSpec(
            spec.name,
            spec.description,
            schema,
            spec.mode,
            spec.enabled_by_default,
        )

    @staticmethod
    def _validate_spec(spec: ToolSpec) -> None:
        if not isinstance(spec.name, str) or not spec.name:
            raise ValueError("tool names must be non-empty strings")
        if not isinstance(spec.description, str):
            raise ValueError("tool descriptions must be strings")
        if spec.mode not in {"parallel", "sequential"}:
            raise ValueError(f"invalid Tool mode: {spec.mode!r}")
        if type(spec.enabled_by_default) is not bool:
            raise ValueError("Tool enabled_by_default must be a boolean")
        schema = spec.schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Tool schema must describe an object")
        unsupported_schema = set(schema) - {
            "type",
            "required",
            "properties",
            "additionalProperties",
        }
        if unsupported_schema:
            raise ValueError(
                f"Tool schema has unsupported constraints: {sorted(unsupported_schema)}"
            )
        required = schema.get("required")
        properties = schema.get("properties")
        if (
            not isinstance(required, list)
            or not all(isinstance(name, str) for name in required)
            or len(required) != len(set(required))
            or not isinstance(properties, dict)
            or not all(
                isinstance(name, str) and isinstance(contract, dict)
                for name, contract in properties.items()
            )
            or not set(required).issubset(properties)
        ):
            raise ValueError("Tool schema requires named properties and a valid required list")
        if schema.get("additionalProperties", False) is not False:
            raise ValueError("Tool schema must set additionalProperties to false")
        supported_types = {"string", "number", "integer", "boolean"}
        for name, contract in properties.items():
            expected = contract.get("type")
            if expected not in supported_types:
                raise ValueError(f"Tool schema property {name!r} has an unsupported type")
            unsupported = set(contract) - {"type", "minimum"}
            if unsupported:
                raise ValueError(
                    f"Tool schema property {name!r} has unsupported constraints: "
                    f"{sorted(unsupported)}"
                )
            minimum = contract.get("minimum")
            if minimum is not None and (
                expected not in {"number", "integer"}
                or isinstance(minimum, bool)
                or not isinstance(minimum, (int, float))
                or (isinstance(minimum, float) and not math.isfinite(minimum))
            ):
                raise ValueError(f"Tool schema property {name!r} has an invalid minimum")
        try:
            json_object_snapshot(schema, label="Tool schema")
        except ValueError as exc:
            raise ValueError(f"Tool schema must be JSON serializable: {exc}") from exc

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

    def validate_call(self, call: ToolCall) -> None:
        """Validate one prepared ToolCall without executing or mutating runtime state."""

        tool = self._tools.get(call.tool_name)
        if tool is None:
            raise InvalidArgumentsError(f"unknown tool: {call.tool_name}")
        if call.tool_name not in self._enabled:
            raise InvalidArgumentsError(f"tool is not enabled: {call.tool_name}")
        self._validate(call.arguments, tool.spec.schema)

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
                if not isinstance(stream, str) or not isinstance(data, str):
                    raise ValueError("Tool progress stream and data must be strings")
                if on_progress is not None:
                    await on_progress(ToolProgress(call.call_id, call.tool_name, stream, data))

            output = await tool.execute(call.arguments, self.environment, cancel_event, report)
            if not isinstance(output, ToolOutput):
                raise ValueError("Tool execute must return ToolOutput")
            safe_output = json_object_snapshot(output.data, label="ToolOutput.data")
            return ToolResult(call.call_id, call.tool_name, "success", safe_output)
        except InvalidArgumentsError as exc:
            return self._error(call, "invalid_arguments", str(exc))
        except CommandTimeoutError as exc:
            return self._error(call, "timeout", str(exc))
        except ToolExecutionError as exc:
            try:
                if not isinstance(exc.code, str) or not exc.code:
                    raise ValueError("ToolExecutionError code must be a non-empty string")
                safe_output = json_object_snapshot(
                    exc.output,
                    label="ToolExecutionError.output",
                )
            except ValueError as output_error:
                return self._error(call, "tool_error", str(output_error))
            return self._error(call, exc.code, str(exc), safe_output)
        except _ToolOwnedCancellationError as exc:
            return self._error(call, "tool_cancelled_by_extension", str(exc))
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
        except Exception as exc:
            return self._error(call, "tool_error", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _error(
        call: ToolCall, code: str, message: str, output: dict[str, Any] | None = None
    ) -> ToolResult:
        return ToolResult(call.call_id, call.tool_name, "error", output, ToolError(code, message))

    @staticmethod
    def _validate(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
        arguments = json_object_snapshot(arguments, label="ToolCall arguments")
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
