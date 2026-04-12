"""
Public implementation of the GDB console widget package.

``GDBWidget`` is tgdb's terminal-backed debugger pane. It owns the pyte screen,
scrollback buffer, scroll mode, and the imperative hooks that TGDBApp wires to
the real GDB PTY.

The heavy-lifting terminal widget lives in ``content.py`` so this module can
stay focused on the public pane interface.
"""

from __future__ import annotations

from typing import Callable, Optional

from textual import events

from ..highlight_groups import HighlightGroups
from ..pane_chrome import PaneBase
from .content import _GDBContent


# ---------------------------------------------------------------------------
# Public pane wrapper
# ---------------------------------------------------------------------------

# Attributes that must be forwarded to _GDBContent on set.
_GDB_DELEGATE_SET = frozenset(
    {
        "send_to_gdb",
        "resize_gdb",
        "on_switch_to_tgdb",
        "imap_feed",
        "imap_replay",
        "gdb_focused",
        "debugwincolor",
        "ignorecase",
        "wrapscan",
        "max_scrollback",
    }
)


class GDBWidget(PaneBase):
    """Render the raw GDB console inside a titled workspace pane.

    Public interface
    ----------------
    ``GDBWidget(hl, max_scrollback=10000, **kwargs)``
        Create the widget.

    After construction, TGDBApp treats the widget as a black box and drives it
    through the delegated content surface. The main integration points are:

    - ``send_to_gdb`` and ``resize_gdb`` callbacks;
    - ``feed_bytes(data)`` for raw PTY output;
    - mode/config flags such as ``gdb_focused``, ``debugwincolor``,
      ``ignorecase``, and ``wrapscan``; and
    - the scroll-mode messages re-exported by the package.

    The delegated surface intentionally preserves the historical
    ``tgdb.gdb_widget`` API so the rest of tgdb does not need to know about the
    internal ``_GDBContent`` implementation widget.
    """

    def __init__(
        self, hl: HighlightGroups, max_scrollback: int = 10000, **kwargs
    ) -> None:
        """Create an empty GDB console pane."""
        super().__init__(hl, **kwargs)
        self._content = _GDBContent(hl, max_scrollback)
        self.can_focus = True

    def title(self) -> Optional[str]:
        return None

    def compose(self):
        yield from super().compose()
        yield self._content

    # ------------------------------------------------------------------
    # Key and refresh delegation
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        self._content.on_key(event)

    def refresh(self, *args, **kwargs):
        if self._content.is_mounted:
            self._content.refresh(*args, **kwargs)
        return super().refresh(*args, **kwargs)

    # ------------------------------------------------------------------
    # Attribute delegation to _GDBContent
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value) -> None:
        if name in _GDB_DELEGATE_SET and "_content" in self.__dict__:
            setattr(self._content, name, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name: str):
        content = self.__dict__.get("_content")
        if content is not None:
            return getattr(content, name)
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
