"""
Public entry point for the context-menu package.

External code should import :class:`ContextMenu`, :class:`ContextMenuItem`, and
the emitted message types from ``tgdb.context_menu``. The caller supplies the
menu tree, opens the widget at screen coordinates, and then treats it as a
black box that owns cascading layout plus mouse/keyboard navigation.
"""

from .menu import ContextMenu, ContextMenuClosed, ContextMenuSelected
from .model import ContextMenuItem

__all__ = [
    "ContextMenu",
    "ContextMenuClosed",
    "ContextMenuItem",
    "ContextMenuSelected",
]
