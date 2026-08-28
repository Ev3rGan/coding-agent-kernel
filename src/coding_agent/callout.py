"""Isolation helpers for synchronous, untrusted Kernel capability callouts.

Callouts run in a worker thread so component cancellation cannot cancel the
authoritative asyncio task. Callbacks must be finite, synchronous, and
thread-safe. They must not use loop-bound asyncio objects or mutate shared state
without their own synchronization. The Host waits for completion; thread
timeouts or forced thread termination are intentionally not provided here.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextvars import copy_context
from threading import Thread
from typing import ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


class SyncCalloutCancellationError(RuntimeError):
    """A synchronous callback tried to use cancellation as control flow."""


def invoke_sync_callout(callback: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run one finite synchronous callback outside the Host/Run asyncio task."""

    results: list[object] = []
    errors: list[BaseException] = []
    context = copy_context()

    def invoke() -> None:
        try:
            results.append(context.run(callback, *args, **kwargs))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=invoke, name="coding-agent-extension-callout")
    worker.start()
    worker.join()
    if errors:
        error = errors[0]
        if isinstance(error, asyncio.CancelledError):
            raise SyncCalloutCancellationError(
                "Synchronous capability callout attempted cancellation"
            ) from error
        raise error
    return cast(R, results[0])


def dispose_awaitable(value: object) -> None:
    """Consume completed borrowed Futures or close owned unscheduled coroutines."""

    if isinstance(value, asyncio.Future):
        if value.done():
            try:
                value.exception()
            except BaseException:
                pass
        return
    if inspect.iscoroutine(value):
        value.close()
