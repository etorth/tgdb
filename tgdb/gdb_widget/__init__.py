"""
Public entry point for the GDB-widget package.

This package preserves the old ``tgdb.gdb_widget`` import surface. External
code should keep importing ``GDBWidget`` and the scroll-mode message types from
here, while the package keeps its screen/scroll helpers private.

``GDBWidget`` is the black-box debugger-console widget: construct it once, wire
its PTY callbacks, then feed raw console bytes into it and let it own
terminal-emulation and scroll-mode behavior.
"""

from .scroll import (
    ScrollModeChange,
    ScrollSearchCancel,
    ScrollSearchCommit,
    ScrollSearchStart,
    ScrollSearchUpdate,
)
from .pane import GDBWidget

__all__ = [
    "GDBWidget",
    "ScrollModeChange",
    "ScrollSearchCancel",
    "ScrollSearchCommit",
    "ScrollSearchStart",
    "ScrollSearchUpdate",
]
