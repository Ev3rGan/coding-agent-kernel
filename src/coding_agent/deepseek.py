"""DeepSeek Chat Completions adapter for the provider-neutral Kernel seam."""

from __future__ import annotations

import asyncio
import codecs
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

import httpx

from coding_agent.events import (
    AssistantMessage,
    ProviderCancelled,
    ProviderContentEnd,
    ProviderContentStart,
    ProviderDone,
    ProviderError,
    ProviderStreamEnd,
    ProviderStreamEvent,
    ProviderStreamStart,
    ProviderTextDelta,
    ProviderTextEnd,
    ProviderTextStart,
    ProviderThinkingDelta,
    ProviderThinkingEnd,
    ProviderThinkingStart,
    ProviderToolCallDelta,
    ProviderToolCallEnd,
    ProviderToolCallStart,
    ProviderUsage,
    ToolResult,
)
from coding_agent.provider import (
    BranchSummaryMessage,
    ProviderRequest,
    ToolResultMessage,
    UserMessage,
)

DEEPSEEK_API_URL: Final = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODELS: Final = ("deepseek-v4-pro", "deepseek-v4-flash")
DEFAULT_DEEPSEEK_MODEL: Final = "deepseek-v4-pro"


class DeepSeekConfigurationError(ValueError):
    """Reject unusable local configuration before any network operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DeepSeekProtocolError(ValueError):
    pass


@dataclass(slots=True)
class _StreamState:
    response_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    thinking_open: bool = False
    text_open: bool = False
    open_tool_calls: set[int] = field(default_factory=set)


class DeepSeekProvider:
    """Normalize the official DeepSeek SSE protocol into ProviderStreamEvents."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        transport: httpx.AsyncBaseTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        api_key = (os.environ if environment is None else environment).get("DEEPSEEK_API_KEY", "")
        if not isinstance(api_key, str) or not api_key.strip():
            raise DeepSeekConfigurationError(
                "deepseek_api_key_missing",
                "DEEPSEEK_API_KEY is required to use the DeepSeek provider.",
            )
        if model not in DEEPSEEK_MODELS:
            raise DeepSeekConfigurationError(
                "deepseek_model_invalid",
                f"DeepSeek model must be one of: {', '.join(DEEPSEEK_MODELS)}.",
            )
        self._api_key = api_key
        self._model = model
        self._transport = transport

    def __repr__(self) -> str:
        return f"DeepSeekProvider(model={self._model!r}, api_key=<redacted>)"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        """Send one request and yield only provider-neutral stream events."""

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(60.0, read=300.0)
        state = _StreamState(model=self._model)
        async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    DEEPSEEK_API_URL,
                    headers=headers,
                    json=_request_body(request, self._model),
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        yield _http_failure(response.status_code)
                        return

                    yield ProviderStreamStart(self._model)
                    yield ProviderContentStart()
                    async for data in _sse_data(response.aiter_bytes()):
                        if data == "[DONE]":
                            if state.finish_reason is None:
                                raise _DeepSeekProtocolError(
                                    "DeepSeek stream ended before a finish reason was received."
                                )
                            async for event in _finish_events(state):
                                yield event
                            return
                        payload = _json_object(data)
                        if payload.get("error") is not None:
                            yield ProviderError(
                                "deepseek_api_error",
                                "DeepSeek API returned an error response; verify request and "
                                "account configuration.",
                            )
                            return
                        async for event in _payload_events(payload, state):
                            yield event
                    yield ProviderError(
                        "provider_unavailable",
                        "DeepSeek stream ended before the [DONE] marker; retry the request.",
                    )
            except asyncio.CancelledError:
                yield ProviderCancelled("DeepSeek stream was cancelled by the Host.")
            except httpx.TimeoutException:
                yield ProviderError(
                    "provider_timeout",
                    "DeepSeek request timed out; retry the request.",
                )
            except httpx.TransportError:
                yield ProviderError(
                    "provider_unavailable",
                    "DeepSeek transport was interrupted; retry the request.",
                )
            except (UnicodeError, json.JSONDecodeError, _DeepSeekProtocolError) as exc:
                yield ProviderError(
                    code="deepseek_invalid_response",
                    message=f"DeepSeek stream was malformed: {type(exc).__name__}: {exc}",
                )


def _request_body(request: ProviderRequest, model: str) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    system_parts = [request.system_prompt]
    if request.tool_guidelines:
        system_parts.append(f"Tool guidelines:\n{request.tool_guidelines}")
    if request.project_context:
        system_parts.append("Project context:\n" + "\n".join(request.project_context))
    system_content = "\n\n".join(part for part in system_parts if part)
    if system_content:
        messages.append({"role": "system", "content": system_content})

    for message in request.messages:
        if isinstance(message, BranchSummaryMessage):
            messages.append(
                {"role": "system", "content": f"Active branch summary:\n{message.text}"}
            )
        elif isinstance(message, UserMessage):
            messages.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            record: dict[str, object] = {"role": "assistant", "content": message.text}
            if message.thinking:
                record["reasoning_content"] = message.thinking
            if message.tool_calls:
                record["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": _canonical_json(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(record)
        elif isinstance(message, ToolResultMessage):
            messages.extend(_tool_result_message(result) for result in message.results)
        else:  # pragma: no cover - ProviderRequest is a closed union
            raise TypeError(f"Unsupported ProviderRequest message: {type(message).__name__}")

    tools = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("schema", {}),
            },
        }
        for tool in request.tools
    ]
    body: dict[str, object] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    if tools:
        body["tools"] = tools
    return body


def _http_failure(status_code: int) -> ProviderError:
    if status_code in {408, 504}:
        return ProviderError(
            "provider_timeout",
            f"DeepSeek request timed out with HTTP {status_code}; retry the request.",
        )
    if status_code == 429:
        return ProviderError(
            "rate_limited",
            "DeepSeek rate limit was reached; retry after a short delay.",
        )
    if status_code in {500, 503}:
        return ProviderError(
            "provider_unavailable",
            f"DeepSeek is unavailable (HTTP {status_code}); retry the request.",
        )
    if status_code == 401:
        return ProviderError(
            "authentication_failed",
            "DeepSeek authentication failed; verify DEEPSEEK_API_KEY.",
        )
    if status_code == 402:
        return ProviderError(
            "insufficient_balance",
            "DeepSeek account balance is insufficient; check the account before retrying.",
        )
    if status_code in {400, 422}:
        return ProviderError(
            "invalid_request",
            f"DeepSeek rejected the request format or parameters (HTTP {status_code}).",
        )
    return ProviderError(
        "deepseek_http_error",
        f"DeepSeek request failed with HTTP {status_code}.",
    )


def _tool_result_message(result: ToolResult) -> dict[str, object]:
    content = {
        "tool_name": result.tool_name,
        "status": result.status,
        "output": result.output,
        "error": (
            None
            if result.error is None
            else {"code": result.error.code, "message": result.error.message}
        ),
    }
    return {
        "role": "tool",
        "tool_call_id": result.call_id,
        "content": _canonical_json(content),
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _sse_data(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    data_lines: list[str] = []
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            if line == "data":
                data_lines.append("")
            elif line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
    buffer += decoder.decode(b"", final=True)
    if buffer or data_lines:
        raise _DeepSeekProtocolError("DeepSeek SSE stream ended in the middle of an event.")


def _json_object(data: str) -> dict[str, Any]:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise _DeepSeekProtocolError("DeepSeek SSE data must decode to an object.")
    return cast(dict[str, Any], value)


async def _payload_events(
    payload: dict[str, Any], state: _StreamState
) -> AsyncIterator[ProviderStreamEvent]:
    response_id = payload.get("id")
    model = payload.get("model")
    if response_id is not None:
        if not isinstance(response_id, str):
            raise _DeepSeekProtocolError("DeepSeek response id must be a string.")
        if state.response_id is not None and state.response_id != response_id:
            raise _DeepSeekProtocolError("DeepSeek response id changed during the stream.")
        state.response_id = response_id
    if model is not None:
        if not isinstance(model, str):
            raise _DeepSeekProtocolError("DeepSeek model must be a string.")
        state.model = model

    usage = payload.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise _DeepSeekProtocolError("DeepSeek usage must be an object or null.")
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if type(input_tokens) is not int or type(output_tokens) is not int:
            raise _DeepSeekProtocolError("DeepSeek usage token counts must be integers.")
        yield ProviderUsage(input_tokens, output_tokens)

    choices = payload.get("choices")
    if not isinstance(choices, list):
        raise _DeepSeekProtocolError("DeepSeek choices must be an array.")
    if not choices:
        return
    if len(choices) != 1 or not isinstance(choices[0], dict):
        raise _DeepSeekProtocolError("DeepSeek stream must contain exactly one choice.")
    choice = choices[0]
    if choice.get("index") != 0:
        raise _DeepSeekProtocolError("DeepSeek choice index must be zero.")
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        raise _DeepSeekProtocolError("DeepSeek choice delta must be an object.")

    reasoning = delta.get("reasoning_content")
    if reasoning is not None:
        if not isinstance(reasoning, str):
            raise _DeepSeekProtocolError("DeepSeek reasoning delta must be a string or null.")
        if reasoning:
            if not state.thinking_open:
                state.thinking_open = True
                yield ProviderThinkingStart()
            yield ProviderThinkingDelta(reasoning)

    content = delta.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise _DeepSeekProtocolError("DeepSeek content delta must be a string or null.")
        if content:
            if state.thinking_open:
                state.thinking_open = False
                yield ProviderThinkingEnd()
            if not state.text_open:
                state.text_open = True
                yield ProviderTextStart()
            yield ProviderTextDelta(content)

    raw_tool_calls = delta.get("tool_calls")
    if raw_tool_calls is not None:
        if not isinstance(raw_tool_calls, list):
            raise _DeepSeekProtocolError("DeepSeek tool_calls delta must be an array.")
        indexes_in_chunk: set[int] = set()
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, dict):
                raise _DeepSeekProtocolError("DeepSeek ToolCall delta must be an object.")
            index = raw_call.get("index")
            if type(index) is not int or index < 0 or index in indexes_in_chunk:
                raise _DeepSeekProtocolError(
                    "DeepSeek ToolCall index must be a unique non-negative integer per chunk."
                )
            indexes_in_chunk.add(index)
            if index not in state.open_tool_calls:
                state.open_tool_calls.add(index)
                yield ProviderToolCallStart(index)
            call_id = raw_call.get("id")
            call_type = raw_call.get("type")
            if call_id is not None and not isinstance(call_id, str):
                raise _DeepSeekProtocolError("DeepSeek ToolCall id delta must be a string or null.")
            if call_type is not None and call_type != "function":
                raise _DeepSeekProtocolError("DeepSeek supports function ToolCalls only.")
            raw_function = raw_call.get("function", {})
            if not isinstance(raw_function, dict):
                raise _DeepSeekProtocolError("DeepSeek ToolCall function must be an object.")
            name = raw_function.get("name")
            arguments = raw_function.get("arguments")
            if (name is not None and not isinstance(name, str)) or (
                arguments is not None and not isinstance(arguments, str)
            ):
                raise _DeepSeekProtocolError(
                    "DeepSeek ToolCall name and arguments deltas must be strings or null."
                )
            yield ProviderToolCallDelta(
                index,
                call_id_delta=call_id or "",
                tool_name_delta=name or "",
                arguments_delta=arguments or "",
            )

    finish_reason = choice.get("finish_reason")
    if finish_reason is not None:
        if not isinstance(finish_reason, str):
            raise _DeepSeekProtocolError("DeepSeek finish reason must be a string or null.")
        state.finish_reason = finish_reason


async def _finish_events(state: _StreamState) -> AsyncIterator[ProviderStreamEvent]:
    if state.finish_reason == "insufficient_system_resource":
        yield ProviderError(
            "provider_unavailable",
            "DeepSeek stopped because inference resources were unavailable; retry the request.",
        )
        return
    if state.thinking_open:
        yield ProviderThinkingEnd()
    if state.text_open:
        yield ProviderTextEnd()
    for index in sorted(state.open_tool_calls):
        yield ProviderToolCallEnd(index)
    yield ProviderContentEnd()
    stop_reason = "tool_use" if state.finish_reason == "tool_calls" else state.finish_reason
    yield ProviderDone(stop_reason or "stop", state.response_id)
    yield ProviderStreamEnd()
