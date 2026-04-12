"""
Public implementation of the source-widget package.

``SourceView`` is tgdb's source pane. It mirrors cgdb's source-mode behavior
while exposing a stable import surface for the rest of the app: callers create
the widget, then drive it by assigning source/selection state and by handling
the semantic messages it emits.

The heavy-lifting content widget lives in ``content.py`` so this module can stay
focused on the public pane interface.
"""

from __future__ import annotations

from typing import Optional

from textual import events

from ..highlight_groups import HighlightGroups
from ..pane_chrome import PaneBase
from .content import _SourceContent


# ---------------------------------------------------------------------------
# Public pane wrapper
# ---------------------------------------------------------------------------

# Attributes that must be forwarded to _SourceContent on set.
_SRC_DELEGATE_SET = frozenset(
    {
        "exe_line",
        "sel_line",
        "executing_line_display",
        "selected_line_display",
        "tabstop",
        "hlsearch",
        "ignorecase",
        "wrapscan",
        "showmarks",
        "color",
    }
)


class SourceView(PaneBase):
    """Render source code and cgdb-style source-mode interactions.

    Public interface
    ----------------
    ``SourceView(hl, **kwargs)``
        Create the widget.

    Callers typically treat the widget as a black box with a small imperative
    surface:

    - assign ``source_file`` to publish a new file;
    - assign ``exe_line`` and ``sel_line`` to update execution/cursor state;
    - assign options such as ``tabstop``, ``hlsearch``, ``ignorecase``,
      ``wrapscan``, ``showmarks``, and ``color`` to mirror user config; and
    - handle the semantic messages emitted by the content widget, such as
      ``ToggleBreakpoint``, ``OpenFileDialog``, ``SearchStart``, and
      ``GDBCommand``.

    The delegated attribute surface intentionally matches the historical
    ``tgdb.source_widget`` import surface so the rest of tgdb can keep treating
    ``SourceView`` as the single public entry point for source-pane behavior.
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        """Create an empty source pane."""
        super().__init__(hl, **kwargs)
        self._content = _SourceContent(hl)
        self._content._pane = self  # so _content.post_message bubbles through us


    def title(self) -> Optional[str]:
        content = self.__dict__.get("_content")
        if content is not None:
            sf = getattr(content, "source_file", None)
        else:
            sf = None
        if sf is not None:
            return sf.path
        return None


    def align(self) -> str:
        return "left"


    def compose(self):
        yield from super().compose()
        yield self._content

    # ------------------------------------------------------------------
    # Key, resize, refresh delegation
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        self._content.on_key(event)


    def refresh(self, *args, **kwargs):
        # Refresh title bar in case source_file path changed.
        if self._title_bar is not None and self._title_bar.is_mounted:
            self._title_bar.refresh()
        if self._content.is_mounted:
            self._content.refresh(*args, **kwargs)
        return super().refresh(*args, **kwargs)

    # ------------------------------------------------------------------
    # Attribute delegation to _SourceContent
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value) -> None:
        if name in _SRC_DELEGATE_SET and "_content" in self.__dict__:
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
