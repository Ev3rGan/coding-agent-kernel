"""Run-scoped queue and cancellation state owned by one AgentRun."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from itertools import count

from coding_agent.events import PendingMessage, PendingMessageKind
from coding_agent.permissions import (
    PermissionDecision,
    PermissionEvaluation,
    PermissionMode,
    PermissionRequest,
    ToolCallLike,
    make_permission_request,
)
from coding_agent.provider import UserMessage


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Small provider-neutral retry policy with explicit classifications."""

    max_attempts: int = 3
    delay_seconds: float = 0.0
    retryable_codes: frozenset[str] = frozenset(
        {"provider_unavailable", "provider_timeout", "rate_limited"}
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")

    def is_retryable(self, code: str) -> bool:
        return code in self.retryable_codes


class RunControl:
    """The sole mutable owner of both pending queues and cancellation signal.

    All mutations are synchronous and contain no scheduling point. AgentRun serializes
    Host-facing mutations with its lifecycle lock; the Kernel drains snapshots only
    between Provider/Tool awaits, so a message belongs wholly before or after a drain.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._numbers = count(1)
        self._steering: deque[PendingMessage] = deque()
        self._follow_up: deque[PendingMessage] = deque()
        self._permission_numbers = count(1)
        self._pending_permission: (
            tuple[PermissionRequest, asyncio.Future[PermissionDecision]] | None
        ) = None
        self.cancel_event = asyncio.Event()

    def enqueue(self, kind: PendingMessageKind, text: str) -> PendingMessage:
        message = PendingMessage(
            message_id=f"{self._run_id}-{kind.value}-{next(self._numbers)}",
            kind=kind,
            text=text,
        )
        self._queue(kind).append(message)
        return message

    def queue_size(self, kind: PendingMessageKind) -> int:
        return len(self._queue(kind))

    def drain_steering(self) -> tuple[PendingMessage, ...]:
        return self._drain(PendingMessageKind.STEERING)

    def drain_follow_up(self) -> tuple[PendingMessage, ...]:
        return self._drain(PendingMessageKind.FOLLOW_UP)

    def pending_messages(self) -> tuple[UserMessage, ...]:
        return tuple(
            UserMessage(text=message.text) for message in (*self._steering, *self._follow_up)
        )

    def drop_all(self) -> tuple[PendingMessage, ...]:
        dropped = (*self._steering, *self._follow_up)
        self._steering.clear()
        self._follow_up.clear()
        return dropped

    def open_permission(
        self,
        call: ToolCallLike,
        evaluation: PermissionEvaluation,
        mode: PermissionMode,
    ) -> PermissionRequest:
        if self._pending_permission is not None:
            raise RuntimeError("AgentRun already has a pending Permission Request")
        request = make_permission_request(
            run_id=self._run_id,
            ordinal=next(self._permission_numbers),
            mode=mode,
            call=call,
            evaluation=evaluation,
        )
        future: asyncio.Future[PermissionDecision] = asyncio.get_running_loop().create_future()
        self._pending_permission = (request, future)
        return request

    async def wait_for_permission(self, request: PermissionRequest) -> PermissionDecision:
        pending = self._pending_permission
        if pending is None or pending[0] != request:
            raise RuntimeError("Permission Request is no longer pending")
        try:
            return await pending[1]
        finally:
            if self._pending_permission == pending:
                self._pending_permission = None

    def resolve_permission(self, request_id: str, decision: PermissionDecision) -> None:
        request, future = self._pending_for_resolution(request_id)
        if (
            decision.call_id != request.call_id
            or decision.tool_name != request.tool_name
            or decision.mode is not request.mode
            or decision.final_arguments_json != request.final_arguments_json
            or decision.intent != request.intent
            or decision.binding != request.binding
            or decision.source != "host"
        ):
            raise RuntimeError("Permission Decision does not match the pending request")
        future.set_result(decision)

    def validate_permission_resolution(
        self,
        request_id: str,
        approved: bool,
    ) -> PermissionRequest:
        if type(approved) is not bool:
            raise TypeError("Permission resolution must be a boolean")
        request, _ = self._pending_for_resolution(request_id)
        return request

    def _pending_for_resolution(
        self,
        request_id: str,
    ) -> tuple[PermissionRequest, asyncio.Future[PermissionDecision]]:
        pending = self._pending_permission
        if pending is None:
            raise RuntimeError("AgentRun has no pending Permission Request")
        request, future = pending
        if request.request_id != request_id:
            raise RuntimeError("Permission Request is stale or does not match the pending request")
        if future.done():
            raise RuntimeError("Permission Request has already been resolved")
        return request, future

    def invalidate_permission(self) -> PermissionRequest | None:
        pending = self._pending_permission
        self._pending_permission = None
        if pending is None or pending[1].done():
            return None
        pending[1].cancel()
        return pending[0]

    def _drain(self, kind: PendingMessageKind) -> tuple[PendingMessage, ...]:
        queue = self._queue(kind)
        snapshot = tuple(queue)
        queue.clear()
        return snapshot

    def _queue(self, kind: PendingMessageKind) -> deque[PendingMessage]:
        return self._steering if kind is PendingMessageKind.STEERING else self._follow_up
