"""Async helpers shared across the tgdb codebase.

The most important entry point is :func:`supervise`. It wraps an
``asyncio.create_task`` with a done-callback that logs any unhandled
exception via :mod:`tgdb.log`. Use it for *every* coroutine launched from
synchronous code, including the cases where the resulting task is stored
on ``self`` and later cancelled — wrapping in :func:`supervise` is still
correct because cancellation is a no-op for the supervisor (the
done-callback skips cancelled tasks). Plain ``asyncio.create_task`` is
discouraged because it lets exceptions surface only as Python's "Task
exception was never retrieved" warning at GC time, which is easy to miss
in a long debugging session.
"""

import asyncio
import logging
from collections.abc import Awaitable, Coroutine
from typing import Any

_log = logging.getLogger("tgdb.async")


def _on_supervised_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    name = task.get_name()
    _log.error(
        f"unhandled exception in supervised task {name!r}: {exc!r}",
        exc_info=exc,
    )


def supervise(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task:
    """Schedule *coro* on the running loop and log any unhandled exception.

    The returned :class:`asyncio.Task` behaves like one created by
    ``asyncio.create_task``; the only difference is the done-callback
    that turns silent failures into ``ERROR``-level log entries.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_on_supervised_done)
    return task


__all__ = ["supervise"]
