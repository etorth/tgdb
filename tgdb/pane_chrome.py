"""
Shared pane chrome for tgdb workspace widgets.

``PaneBase`` provides the 1-row title bar used by pane-like widgets such as the
source view, GDB console, and the optional auxiliary panes. The title bar is:

- always visible, even when ``title()`` returns ``None``;
- styled with the ``StatusLine`` highlight group by default; and
- usable as a drag handle when the pane sits inside a vertical
  ``PaneContainer``.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import events
from textual.widget import Widget

from .highlight_groups import HighlightGroups
from .pane_utils import center_cells, fit_cells


class _TitleBar(Widget):
    """One-row title bar rendered inside every PaneBase.

    Renders the pane title and acts as a drag handle for resizing
    adjacent panes inside a vertical PaneContainer (duck-typed: any
    parent that has ``_items``, ``orientation == "vertical"`` and
    ``_resize_from_title_drag``).
    """

    DEFAULT_CSS = """
    _TitleBar {
        height: 1;
        width: 1fr;
    }
    """

    def __init__(self, pane: "PaneBase", **kwargs) -> None:
        super().__init__(**kwargs)
        self._pane = pane
        self._dragging = False
        self.can_focus = False


    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        hl = getattr(self._pane, "hl", None)
        if hl is not None:
            style = hl.style(self._pane.color())
        else:
            style = ""
        title = self._pane.title()
        if title:
            align = self._pane.align()
            if align == "center":
                text = center_cells(title, width)
            else:
                text = fit_cells(title, width)
        else:
            text = " " * width
        return Text(text, style=style, no_wrap=True, overflow="crop")

    # ------------------------------------------------------------------
    # Mouse drag — walk up to find the right vertical container
    # ------------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self._dragging = True
            self.capture_mouse()
            event.stop()


    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            self._do_drag(int(event.screen_x), int(event.screen_y))
            event.stop()


    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging and event.button == 1:
            self._dragging = False
            self.release_mouse()
            event.stop()


    def _do_drag(self, screen_x: int, screen_y: int) -> None:
        """Walk up the widget tree to find the first vertical container
        where the pane is NOT the first item, then trigger a resize."""
        pane = self._pane
        child = pane
        while child.parent is not None:
            parent = child.parent
            # Duck-type check for PaneContainer with vertical orientation
            if (
                getattr(parent, "orientation", None) == "vertical"
                and hasattr(parent, "_items")
                and hasattr(parent, "_resize_from_title_drag")
            ):
                items = parent._items
                if child in items:
                    idx = items.index(child)
                    if idx > 0:
                        parent._resize_from_title_drag(
                            items[idx - 1], items[idx], screen_y
                        )
                        return
                    # idx == 0 → this pane is first; keep walking up
            child = parent


class PaneBase(Widget):
    """Base class for all tgdb pane widgets.

    Subclasses MUST NOT define their own ``compose()`` without calling
    ``yield from super().compose()`` first so that the title bar is
    always the first child.

    Typical subclass pattern::

        class MyPane(PaneBase):
            def title(self) -> str: return "My Title"
            def compose(self):
                yield from super().compose()
                yield _MyContentWidget(self.hl)
    """

    DEFAULT_CSS = """
    PaneBase {
        width: 1fr;
        height: 1fr;
        layout: vertical;
        overflow: hidden;
        min-width: 4;
        min-height: 2;
    }
    """

    def __init__(self, hl: Optional[HighlightGroups] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._title_bar: Optional[_TitleBar] = None


    def title(self) -> Optional[str]:
        """Text shown in the title bar.  None renders a blank bar."""
        return None


    def color(self) -> str:
        """Highlight-group name used to style the title bar background."""
        return "StatusLine"


    def align(self) -> str:
        """Alignment of the title text: ``"left"``, ``"center"``, or ``"right"``."""
        return "center"


    def compose(self):
        self._title_bar = _TitleBar(self)
        yield self._title_bar


    def refresh_title(self) -> None:
        """Refresh the title bar (call when title() output may have changed)."""
        if self._title_bar is not None and self._title_bar.is_mounted:
            self._title_bar.refresh()
