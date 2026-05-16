"""Domain-specific exceptions for GDB controller requests.

These exception types are intentionally plain ``Exception`` subclasses —
they do **not** inherit from ``asyncio.CancelledError`` or
``asyncio.TimeoutError``.  This keeps them out of asyncio's internal
cancellation machinery so that ``task.cancel()`` (used during app
shutdown) propagates cleanly through ``asyncio.CancelledError`` without
being caught by domain-level handlers.

Callers that need to distinguish "GDB cancelled this request because
the inferior resumed" from "the asyncio task is being torn down" can
catch ``GDBRequestCancelled`` and let ``asyncio.CancelledError`` pass
through.
"""


class GDBRequestCancelled(Exception):
    """A GDB convenience function request was cancelled.

    Raised when the GDB-side convenience function returns ``"cancelled"``
    — typically because tgdb sent a cancel token after the inferior
    resumed or a newer request of the same type superseded this one.
    """


class GDBRequestTimeout(Exception):
    """An MI command did not complete within its timeout window.

    Raised when ``asyncio.wait_for`` fires before both the MI response
    and (for socket-based commands) the socket data payload arrive.
    For convenience function commands, a cancel token is sent to GDB
    before raising so the GDB side can clean up.
    """


class GDBRequestFailed(Exception):
    """A GDB convenience function or MI command reported failure.

    Raised when the MI response is ``^error`` for a convenience function
    call, or when the convenience function itself returns ``"failed"``.
    The exception message carries the GDB error string when available.
    """
