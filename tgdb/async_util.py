"""Async helpers shared across the tgdb codebase.

``_on_task_done`` is a :class:`asyncio.Task` done-callback that logs any
unhandled exception at ``ERROR`` level via :mod:`tgdb.log`.  Attach it to
tasks created with :func:`asyncio.create_task` or :class:`asyncio.Task`
in the few places where a synchronous caller truly cannot ``await`` the
result (e.g. sync→async bridge in a Textual handler that needs a task
reference for later cancellation).  Most coroutines should simply be
``await``-ed directly.

``spawn_eager_task`` creates an eager-started :class:`asyncio.Task`.  If
the coroutine completes synchronously (no real suspend), the task is
already done when it returns and no bookkeeping is needed.  Otherwise
the task is kept alive in *task_set* and automatically removed on
completion.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger("tgdb.async")


def _on_task_done(task: asyncio.Task) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    name = task.get_name()
    _log.error(
        f"unhandled exception in task {name!r}: {exc!r}",
        exc_info=exc,
    )


def spawn_eager_task(
    coro: Coroutine[Any, Any, Any],
    task_set: set[asyncio.Task],
    *,
    name: str | None = None,
) -> asyncio.Task:
    """Create an eager-started task; track it in *task_set* if it suspends.

    If the coroutine completes without hitting a real ``await`` suspend
    point the task is already done when this function returns and nothing
    is added to *task_set*.  Otherwise the task is inserted into
    *task_set* and automatically removed (via a done-callback) when it
    finishes.

    Unhandled exceptions are logged at ``ERROR`` level.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.Task(coro, loop=loop, eager_start=True, name=name)
    if task.done():
        exc = None
        if not task.cancelled():
            exc = task.exception()
        if exc is not None:
            _log.error(
                f"unhandled exception in task {name!r}: {exc!r}",
                exc_info=exc,
            )
        return task
    task_set.add(task)
    task.add_done_callback(task_set.discard)
    task.add_done_callback(_on_task_done)
    return task


__all__ = ["_on_task_done", "spawn_eager_task"]
