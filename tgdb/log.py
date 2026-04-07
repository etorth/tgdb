"""
Centralised logging for tgdb.

Usage
-----
Initialise once at startup (from __main__.py) with the path supplied by
``--log``.  Every other module imports ``get_logger()`` and calls it each
time they need to log — this avoids holding a module-level reference that
would be a no-op logger if logging is initialised later.

    from .log import get_logger

    get_logger().debug("GDB started, pid=%d", pid)

If ``--log`` is not given, ``init()`` is never called and ``get_logger()``
returns a ``logging.Logger`` whose only handler is a ``NullHandler``, so
every log call is a cheap no-op.

Log levels used in tgdb
-----------------------
DEBUG   — low-level MI traffic, varobj operations, key dispatching
INFO    — significant lifecycle events (GDB started/stopped, file loaded,
          breakpoint set, command executed)
WARNING — recoverable unexpected conditions (MI parse errors, missing
          callbacks, out-of-range values)
ERROR   — failures that degrade functionality (MI channel closed
          unexpectedly, PTY spawn failure, file not found)
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "tgdb"

# The root tgdb logger. By default it has only a NullHandler so no output
# is produced unless init() is called.
_logger = logging.getLogger(_LOGGER_NAME)
_logger.addHandler(logging.NullHandler())
_logger.setLevel(logging.DEBUG)
_logger.propagate = False


def init(log_file: str) -> None:
    """Attach a file handler to the tgdb logger.

    Call this once from ``__main__.py`` when ``--log`` is provided.
    Subsequent calls replace the existing file handler.
    """
    # Remove any previously attached file handlers (in case of re-init).
    for handler in list(_logger.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
            _logger.removeHandler(handler)

    try:
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    except OSError as e:
        print(f"tgdb: cannot open log file {log_file!r}: {e}", file=sys.stderr)
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh.setFormatter(fmt)
    _logger.addHandler(fh)
    _logger.info("tgdb logging started — writing to %s", log_file)


def get_logger() -> logging.Logger:
    """Return the tgdb root logger.

    Sub-loggers for individual modules are children of this logger and
    inherit its handlers automatically.

        log = logging.getLogger("tgdb.gdb_controller")

    is equivalent to calling ``get_logger().getChild("gdb_controller")``.
    """
    return _logger
