"""Async helpers shared across the tgdb codebase.

The most important entry point is :func:`supervise`. It wraps an
``asyncio.create_task`` with a done-callback that logs any unhandled
exception via :mod:`tgdb.log`. Use it whenever a coroutine has to be
launched from synchronous code (the most common case is callbacks driven
by ``loop.add_reader`` on the GDB MI fd) — the returned task is otherwise
a perfectly normal ``asyncio.Task`` so it can also be stored, awaited, or
cancelled by the caller.

Plain ``asyncio.create_task(coro)`` is still appropriate when the result
is stored in an attribute *and* the caller eventually awaits or cancels
it (for example ``self._gdb_task`` in :class:`AppCoreMixin`). The point
of :func:`supervise` is to make sure that "fire-and-forget" coroutines
never silently swallow exceptions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Coroutine, Optional

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
    name: Optional[str] = None,
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
