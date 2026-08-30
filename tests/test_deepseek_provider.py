from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from coding_agent.control import RetryPolicy
from coding_agent.deepseek import DeepSeekConfigurationError, DeepSeekProvider
from coding_agent.environment import LocalCodingEnvironment
from coding_agent.events import (
    AgentRunState,
    AgentSessionEvent,
    AgentSessionEventKind,
    AssistantMessage,
    AssistantMessageAccumulator,
    ProviderCancelled,
    ProviderDone,
    ProviderError,
    ProviderEventKind,
    ProviderStreamEvent,
    ProviderStreamStart,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderUsage,
    ToolCall,
    ToolError,
    ToolResult,
)
from coding_agent.extensions import ExtensionRegistry, Hook, ToolCallHookInput, Transform
from coding_agent.kernel import AgentKernel
from coding_agent.permissions import PermissionMode
from coding_agent.provider import (
    BranchSummaryMessage,
    ProviderRequest,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.session import JsonlSessionStore, Session
from coding_agent.tool_runtime import ToolRuntime


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _InterruptingStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first_chunk
        raise httpx.ReadError("connection dropped")

    async def aclose(self) -> None:
        self.closed = True


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keeps this an async generator
            yield b""

    async def aclose(self) -> None:
        self.closed = True


class _RewriteDeepSeekToolCall:
    name = "rewrite-deepseek-tool-call"

    def register(self, registry: ExtensionRegistry) -> None:
        registry.register_hook(Hook.TOOL_CALL, self._rewrite)

    def _rewrite(self, hook_input: ToolCallHookInput) -> Transform[ToolCall]:
        arguments = hook_input.arguments
        arguments["path"] = "after.txt"
        return Transform(ToolCall(hook_input.call_id, hook_input.tool_name, arguments))


def _split_bytes(payload: bytes, cuts: tuple[int, ...]) -> tuple[bytes, ...]:
    boundaries = (0, *cuts, len(payload))
    return tuple(payload[start:end] for start, end in zip(boundaries, boundaries[1:], strict=False))


def _sse(*items: dict[str, Any] | str) -> bytes:
    return b"".join(
        b"data: "
        + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)).encode()
        + b"\r\n\r\n"
        for item in items
    )


def _provider_events(
    provider: DeepSeekProvider,
    request: ProviderRequest | None = None,
) -> list[ProviderStreamEvent]:
    async def collect() -> list[ProviderStreamEvent]:
        return [event async for event in provider.stream(request or ProviderRequest(messages=()))]

    return asyncio.run(collect())


def test_provider_maps_complete_request_and_normalizes_stream_across_byte_boundaries() -> None:
    captured: dict[str, Any] = {}
    stream = _sse(
        {
            "id": "response-1",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "reasoning_content": "先想"},
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "response-1",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "答案"},
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "response-1",
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 23, "completion_tokens": 7, "total_tokens": 30},
        },
        "[DONE]",
    )
    reasoning_start = stream.index("先".encode())
    text_start = stream.index("答".encode())
    chunk_stream = _ChunkStream(
        _split_bytes(
            stream,
            (
                1,
                5,
                reasoning_start + 1,
                reasoning_start + 2,
                text_start + 1,
                text_start + 2,
                len(stream) - 3,
            ),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads((await request.aread()).decode())
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=chunk_stream,
        )

    request = ProviderRequest(
        system_prompt="system",
        tool_guidelines="use tools safely",
        project_context=("project A", "project B"),
        tools=(
            {
                "name": "read",
                "description": "Read a file.",
                "schema": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string"}},
                    "additionalProperties": False,
                },
                "mode": "parallel",
            },
        ),
        messages=(
            BranchSummaryMessage(text="older branch summary"),
            UserMessage(text="inspect the file"),
            AssistantMessage(
                text="",
                thinking="need the file",
                tool_calls=(ToolCall("call-1", "read", {"path": "sample.py"}),),
            ),
            ToolResultMessage(
                results=(
                    ToolResult(
                        "call-1",
                        "read",
                        "error",
                        error=ToolError("missing", "sample.py was not found"),
                    ),
                )
            ),
        ),
    )
    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider, request)

    assert captured["authorization"] == "Bearer test-only-secret"
    body = captured["body"]
    assert body["model"] == "deepseek-v4-pro"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file.",
                "parameters": request.tools[0]["schema"],
            },
        }
    ]
    assert body["messages"] == [
        {
            "role": "system",
            "content": (
                "system\n\nTool guidelines:\nuse tools safely\n\n"
                "Project context:\nproject A\nproject B"
            ),
        },
        {"role": "system", "content": "Active branch summary:\nolder branch summary"},
        {"role": "user", "content": "inspect the file"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "need the file",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"sample.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": (
                '{"error":{"code":"missing","message":"sample.py was not found"},'
                '"output":null,"status":"error","tool_name":"read"}'
            ),
        },
    ]
    assert isinstance(events[0], ProviderStreamStart)
    assert events[0].model == "deepseek-v4-pro"
    assert [event.delta for event in events if isinstance(event, ProviderThinkingDelta)] == ["先想"]
    assert [event.delta for event in events if isinstance(event, ProviderTextDelta)] == ["答案"]
    assert [event for event in events if isinstance(event, ProviderUsage)] == [ProviderUsage(23, 7)]
    assert [event for event in events if isinstance(event, ProviderDone)] == [
        ProviderDone("stop", "response-1")
    ]
    assert [event.kind for event in events] == [
        ProviderEventKind.STREAM_START,
        ProviderEventKind.CONTENT_START,
        ProviderEventKind.THINKING_START,
        ProviderEventKind.THINKING_DELTA,
        ProviderEventKind.THINKING_END,
        ProviderEventKind.TEXT_START,
        ProviderEventKind.TEXT_DELTA,
        ProviderEventKind.USAGE,
        ProviderEventKind.TEXT_END,
        ProviderEventKind.CONTENT_END,
        ProviderEventKind.DONE,
        ProviderEventKind.STREAM_END,
    ]
    assert chunk_stream.closed is True


def test_provider_emits_interleaved_incremental_tool_calls_for_canonical_assembly() -> None:
    stream = _sse(
        {
            "id": "tools-1",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "bash", "arguments": '{"command":"'},
                            },
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"'},
                            },
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "tools-1",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'sample.py"}'}},
                            {"index": 1, "function": {"arguments": 'python -V"}'}},
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "tools-1",
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": None,
        },
        "[DONE]",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkStream((stream,)))

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    accumulator = AssistantMessageAccumulator()
    for event in events:
        accumulator.apply(event)

    assert accumulator.message.tool_calls == (
        ToolCall("call-1", "read", {"path": "sample.py"}),
        ToolCall("call-2", "bash", {"command": "python -V"}),
    )
    assert accumulator.message.stop_reason == "tool_use"


def test_provider_accepts_null_fields_in_tool_call_continuations() -> None:
    stream = _sse(
        {
            "id": "nullable-tool-delta",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "nullable-tool-delta",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": None,
                                "type": None,
                                "function": {"name": None, "arguments": None},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "nullable-tool-delta",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": None,
                                "type": None,
                                "function": {"name": None, "arguments": 'sample.py"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            "usage": None,
        },
        {
            "id": "nullable-tool-delta",
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": None,
        },
        "[DONE]",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkStream((stream,)))

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    accumulator = AssistantMessageAccumulator()
    for event in events:
        accumulator.apply(event)

    assert not any(isinstance(event, ProviderError) for event in events)
    assert accumulator.message.tool_calls == (ToolCall("call-1", "read", {"path": "sample.py"}),)
    assert accumulator.message.stop_reason == "tool_use"
    assert [event.kind for event in events] == [
        ProviderEventKind.STREAM_START,
        ProviderEventKind.CONTENT_START,
        ProviderEventKind.TOOL_CALL_START,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_END,
        ProviderEventKind.CONTENT_END,
        ProviderEventKind.DONE,
        ProviderEventKind.STREAM_END,
    ]


@pytest.mark.parametrize(
    ("status", "expected_code"),
    (
        (400, "invalid_request"),
        (401, "authentication_failed"),
        (402, "insufficient_balance"),
        (422, "invalid_request"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
    ),
)
def test_http_failures_are_actionable_classified_and_secret_free(
    status: int, expected_code: str
) -> None:
    secret = "test-only-secret-never-publish"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": f"server echoed {secret}"}},
        )

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": secret},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    error = next(event for event in events if isinstance(event, ProviderError))
    assert error.code == expected_code
    assert secret not in error.message
    assert secret not in repr(provider)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        (httpx.ReadTimeout("timed out"), "provider_timeout"),
        (httpx.ReadError("stream interrupted"), "provider_unavailable"),
    ),
)
def test_transport_failures_use_existing_retry_classifications(
    failure: httpx.TransportError, expected_code: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    assert [event.code for event in events if isinstance(event, ProviderError)] == [expected_code]


def test_malformed_sse_json_is_a_structured_failure_and_closes_the_stream() -> None:
    stream = _ChunkStream((b"data: {not-json}\n\n",))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    assert [event.code for event in events if isinstance(event, ProviderError)] == [
        "deepseek_invalid_response"
    ]
    assert stream.closed is True


def test_sse_api_error_object_is_distinct_and_does_not_echo_server_content() -> None:
    secret = "test-only-secret-in-api-error"
    stream = _ChunkStream(
        (
            _sse(
                {
                    "error": {
                        "message": f"server echoed {secret}",
                        "type": "invalid_request_error",
                        "code": "bad_request",
                    }
                }
            ),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": secret},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    error = next(event for event in events if isinstance(event, ProviderError))
    assert error.code == "deepseek_api_error"
    assert secret not in error.message


def test_missing_api_key_fails_before_transport_creation() -> None:
    with pytest.raises(DeepSeekConfigurationError) as caught:
        DeepSeekProvider(environment={})

    assert caught.value.code == "deepseek_api_key_missing"
    assert "DEEPSEEK_API_KEY" in str(caught.value)


def test_stream_interruption_reuses_the_existing_kernel_retry_path() -> None:
    interrupted = _InterruptingStream(
        _sse(
            {
                "id": "partial",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "partial"},
                        "finish_reason": None,
                    }
                ],
                "usage": None,
            }
        )
    )
    success = _ChunkStream(
        (
            _sse(
                {
                    "id": "retry-success",
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "recovered"},
                            "finish_reason": None,
                        }
                    ],
                    "usage": None,
                },
                {
                    "id": "retry-success",
                    "model": "deepseek-v4-pro",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": None,
                },
                "[DONE]",
            ),
        )
    )
    request_bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(await request.aread())
        stream = interrupted if len(request_bodies) == 1 else success
        return httpx.Response(200, stream=stream)

    kernel = AgentKernel(
        DeepSeekProvider(
            environment={"DEEPSEEK_API_KEY": "test-only-secret"},
            transport=httpx.MockTransport(handler),
        ),
        retry_policy=RetryPolicy(max_attempts=2),
    )

    async def collect() -> tuple[list[AgentSessionEvent], AgentRunState, str]:
        run = kernel.create_run("retry the DeepSeek stream")
        events = [event async for event in run]
        result = await run.result()
        return events, result.state, "" if result.message is None else result.message.text

    events, state, text = asyncio.run(collect())
    assert state is AgentRunState.SETTLED
    assert text == "recovered"
    assert [event.kind for event in events].count(AgentSessionEventKind.PROVIDER_RETRY) == 1
    assert request_bodies[0] == request_bodies[1]
    assert interrupted.closed is True
    assert success.closed is True


def test_clean_eof_before_done_is_a_retryable_interruption() -> None:
    stream = _ChunkStream(
        (
            _sse(
                {
                    "id": "missing-done",
                    "model": "deepseek-v4-pro",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": None,
                }
            ),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    events = _provider_events(provider)
    assert [event.code for event in events if isinstance(event, ProviderError)] == [
        "provider_unavailable"
    ]


def test_resource_failure_preempts_partial_tool_call_and_reuses_kernel_retry() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            stream=_ChunkStream(
                (
                    _sse(
                        {
                            "id": f"resource-failure-{attempts}",
                            "model": "deepseek-v4-pro",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "partial-call",
                                                "type": "function",
                                                "function": {
                                                    "name": "read",
                                                    "arguments": '{"path":',
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                            "usage": None,
                        },
                        {
                            "id": f"resource-failure-{attempts}",
                            "model": "deepseek-v4-pro",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "insufficient_system_resource",
                                }
                            ],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                        },
                        "[DONE]",
                    ),
                )
            ),
        )

    kernel = AgentKernel(
        DeepSeekProvider(
            environment={"DEEPSEEK_API_KEY": "test-only-secret"},
            transport=httpx.MockTransport(handler),
        ),
        retry_policy=RetryPolicy(max_attempts=2),
    )

    async def run_once() -> tuple[list[AgentSessionEvent], AgentRunState, str | None]:
        run = kernel.create_run("retry a resource-interrupted ToolCall")
        events = [event async for event in run]
        result = await run.result()
        return events, result.state, None if result.error is None else result.error.code

    events, state, error_code = asyncio.run(run_once())
    assert attempts == 2
    assert state is AgentRunState.FAILED
    assert error_code == "provider_unavailable"
    assert [event.kind for event in events].count(AgentSessionEventKind.PROVIDER_RETRY) == 1
    assert not any(event.kind is AgentSessionEventKind.TOOL_EXECUTION_START for event in events)


def test_cancellation_is_normalized_and_closes_the_http_stream() -> None:
    stream = _BlockingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    provider = DeepSeekProvider(
        environment={"DEEPSEEK_API_KEY": "test-only-secret"},
        transport=httpx.MockTransport(handler),
    )

    async def cancel_read() -> object:
        events = cast(
            AsyncGenerator[ProviderStreamEvent, None],
            provider.stream(ProviderRequest(messages=())),
        )
        assert isinstance(await anext(events), ProviderStreamStart)
        await anext(events)

        async def read_one() -> ProviderStreamEvent:
            return await anext(events)

        pending = asyncio.create_task(read_one())
        await stream.started.wait()
        pending.cancel()
        cancelled = await pending
        await events.aclose()
        return cancelled

    event = asyncio.run(cancel_read())
    assert isinstance(event, ProviderCancelled)
    assert stream.closed is True


def test_kernel_cancel_reuses_agent_run_terminal_and_resource_cleanup() -> None:
    stream = _BlockingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    kernel = AgentKernel(
        DeepSeekProvider(
            environment={"DEEPSEEK_API_KEY": "test-only-secret"},
            transport=httpx.MockTransport(handler),
        )
    )

    async def cancel_run() -> tuple[AgentRunState, list[AgentSessionEventKind]]:
        run = kernel.create_run("cancel the live DeepSeek turn")
        observed: list[AgentSessionEvent] = []

        async def observe() -> None:
            async for event in run:
                observed.append(event)

        observer = asyncio.create_task(observe())
        await stream.started.wait()
        result = await run.cancel()
        await observer
        return result.state, [
            event.kind
            for event in observed
            if event.kind
            in {
                AgentSessionEventKind.RUN_SETTLED,
                AgentSessionEventKind.RUN_CANCELLED,
                AgentSessionEventKind.RUN_FAILED,
            }
        ]

    state, terminal_events = asyncio.run(cancel_run())
    assert state is AgentRunState.CANCELLED
    assert terminal_events == [AgentSessionEventKind.RUN_CANCELLED]
    assert stream.closed is True


def test_malformed_tool_call_fails_before_execution_and_session_remains_resumable(
    tmp_path: Path,
) -> None:
    stream = _ChunkStream(
        (
            _sse(
                {
                    "id": "malformed-tool",
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "bad-call",
                                        "type": "function",
                                        "function": {
                                            "name": "read",
                                            "arguments": '{"path":',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                    "usage": None,
                },
                {
                    "id": "malformed-tool",
                    "model": "deepseek-v4-pro",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": None,
                },
                "[DONE]",
            ),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    store = JsonlSessionStore(tmp_path / "session.jsonl")
    kernel = AgentKernel.with_new_session(
        DeepSeekProvider(
            environment={"DEEPSEEK_API_KEY": "test-only-secret"},
            transport=httpx.MockTransport(handler),
        ),
        store,
        configuration={"provider": "deepseek"},
        session_id="malformed-tool-session",
    )

    async def collect() -> tuple[list[AgentSessionEvent], str | None]:
        run = kernel.create_run("attempt malformed tool")
        events = [event async for event in run]
        result = await run.result()
        return events, None if result.error is None else result.error.code

    events, error_code = asyncio.run(collect())
    assert error_code == "provider_exception"
    assert not any(event.kind is AgentSessionEventKind.TOOL_EXECUTION_START for event in events)
    kernel.close_session()
    resumed = Session.resume(store, "malformed-tool-session")
    resumed.close()


def test_deepseek_tool_call_uses_extension_rewrite_then_final_permission_binding(
    tmp_path: Path,
) -> None:
    first = _ChunkStream(
        (
            _sse(
                {
                    "id": "rewrite-tool",
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "rewrite-call",
                                        "type": "function",
                                        "function": {
                                            "name": "write",
                                            "arguments": (
                                                '{"path":"before.txt","content":"rewritten"}'
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                    "usage": None,
                },
                {
                    "id": "rewrite-tool",
                    "model": "deepseek-v4-pro",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": None,
                },
                "[DONE]",
            ),
        )
    )
    second = _ChunkStream(
        (
            _sse(
                {
                    "id": "rewrite-final",
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "rewrite complete"},
                            "finish_reason": None,
                        }
                    ],
                    "usage": None,
                },
                {
                    "id": "rewrite-final",
                    "model": "deepseek-v4-pro",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": None,
                },
                "[DONE]",
            ),
        )
    )
    responses = iter((first, second))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=next(responses))

    kernel = AgentKernel(
        DeepSeekProvider(
            environment={"DEEPSEEK_API_KEY": "test-only-secret"},
            transport=httpx.MockTransport(handler),
        ),
        tool_runtime=ToolRuntime(LocalCodingEnvironment(tmp_path)),
        extensions=(_RewriteDeepSeekToolCall(),),
    )

    async def run_once() -> tuple[AgentRunState, list[str]]:
        run = kernel.create_run("rewrite then authorize", permission_mode=PermissionMode.ASK)
        requested_paths: list[str] = []
        async for event in run:
            if event.permission_request is not None:
                requested_paths.append(str(event.permission_request.final_arguments["path"]))
                await run.resolve_permission(event.permission_request.request_id, True)
        return (await run.result()).state, requested_paths

    state, paths = asyncio.run(run_once())
    assert state is AgentRunState.SETTLED
    assert paths == ["after.txt"]
    assert not (tmp_path / "before.txt").exists()
    assert (tmp_path / "after.txt").read_text(encoding="utf-8") == "rewritten"
