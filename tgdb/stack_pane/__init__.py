"""
Public entry point for the stack-pane package.

External code should import :class:`StackPane` from ``tgdb.stack_pane``. The
caller creates the widget once, then publishes parsed frame snapshots through
``set_frames(...)``. The pane owns rendering, clipping, and selected-frame
highlighting.
"""

from .pane import StackPane

__all__ = ["StackPane"]
