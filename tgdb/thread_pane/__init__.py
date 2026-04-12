"""
Public entry point for the thread-pane package.

External code should import :class:`ThreadPane` from ``tgdb.thread_pane``. The
caller creates the widget once, then publishes parsed thread snapshots through
``set_threads(...)``. The pane owns row formatting and current-thread
highlighting.
"""

from .pane import ThreadPane

__all__ = ["ThreadPane"]
