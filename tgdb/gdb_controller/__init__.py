"""
Public entry point for the GDB-controller package.

External code should import :class:`GDBController` and the structured debugger
record types from ``tgdb.gdb_controller``. The controller is a black-box bridge
between tgdb and GDB: callers construct it, assign the documented callbacks,
start it, and then drive it through console input plus the exposed MI helper
methods.
"""

from .controller import GDBController
from .types import Breakpoint, Frame, LocalVariable, RegisterInfo, ThreadInfo

__all__ = [
    "Breakpoint",
    "Frame",
    "GDBController",
    "LocalVariable",
    "RegisterInfo",
    "ThreadInfo",
]
