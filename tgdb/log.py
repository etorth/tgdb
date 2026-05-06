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

import logging
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

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

    # Best-effort create the log file's parent directory.  Without this,
    # ``--log /var/log/tgdb/session.log`` (or any path under a directory
    # the user hasn't created) raises FileNotFoundError out of
    # ``FileHandler.__init__`` and tgdb dies before it has a chance to
    # render any UI.  Failure of the mkdir itself (e.g. permission denied
    # on the parent's parent) is surfaced through the existing OSError
    # handler below as part of the FileHandler construction failure.
    try:
        parent = Path(log_file).parent
        if parent and parent != Path():
            parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"tgdb: cannot open log file {log_file!r}: {e}") from e

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    start_time = datetime.now(timezone.utc).astimezone()
    _logger.info("=" * 60)
    _logger.info("tgdb session started")
    _logger.info(f"  time    : {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    _logger.info(f"  pid     : {os.getpid()}")
    _logger.info(f"  python  : {sys.version.replace('\n', ' ')}")
    _logger.info(f"  platform: {platform.platform()}")
    _logger.info(f"  argv    : {' '.join(sys.argv)}")
    _logger.info(f"  log file: {log_file}")
    _logger.info("=" * 60)


def shutdown() -> None:
    """Log a session-end marker and flush all handlers.

    Call this from ``__main__.py`` after ``app.run()`` returns so the
    log always has a clear end boundary even when tgdb exits normally.
    """
    end_time = datetime.now(timezone.utc).astimezone()
    _logger.info("=" * 60)
    _logger.info("tgdb session ended")
    _logger.info(f"  time: {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    _logger.info("=" * 60)
    for handler in _logger.handlers:
        handler.flush()


def get_logger() -> logging.Logger:
    """Return the tgdb root logger.

    Sub-loggers for individual modules are children of this logger and
    inherit its handlers automatically.

        log = logging.getLogger("tgdb.gdb_controller")

    is equivalent to calling ``get_logger().getChild("gdb_controller")``.
    """
    return _logger
