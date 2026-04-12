"""
Public entry point for the memory-pane package.

External code should import :class:`MemoryPane` from ``tgdb.memory_pane``. The
caller creates the widget, injects one async memory-read callback through
``set_read_fn(...)``, and requests new dumps through ``set_address(...)``.
"""

from .pane import MemoryPane

__all__ = ["MemoryPane"]
