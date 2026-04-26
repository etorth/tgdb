"""Floating completion popup for ``CommandLineBar``.

Public interface
----------------
``CompletionPopup(hl, **kwargs)``
    Construct the floating popup widget. It is hidden until ``open`` is called.

``open(items, selected_idx, anchor_x, anchor_y, *, max_rows=10)``
    Show the popup with the given candidate list. ``anchor_x`` is the screen
    column where the leftmost cell of the popup should sit; ``anchor_y`` is
    the screen row of the *anchor* (the command-line bar). The popup is laid
    out so that its bottom edge sits one row above ``anchor_y`` — i.e. it
    floats UPWARD from the bar.

``set_selection(selected_idx)``
    Highlight a different row, scrolling the visible window if needed.

``close()``
    Hide the popup.

Callers should treat the widget as a black box. It owns layout, positioning,
the visible-window scroll, and rendering. It does not steal focus and does
not handle keystrokes — the command-line bar continues to drive Tab /
Shift-Tab / Enter / Escape.
"""

from rich.segment import Segment
from rich.style import Style as RichStyle
from textual.strip import Strip
from textual.widget import Widget

from ..highlight_groups import HighlightGroups


class CompletionPopup(Widget):
    """Single-column floating popup listing tab-completion candidates."""

    DEFAULT_CSS = """
    CompletionPopup {
        layer: dialog;
        position: absolute;
        width: 1;
        height: 1;
        display: none;
        background: transparent;
    }
    CompletionPopup.visible {
        display: block;
    }
    """


    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._items: list[str] = []
        self._selected: int = 0
        self._scroll: int = 0
        self._popup_w: int = 1
        self._popup_h: int = 1
        # Anchor column the caller asked for; we may shift left if the popup
        # would overflow the screen width.
        self._anchor_x: int = 0
        self._anchor_y: int = 0


    @property
    def is_open(self) -> bool:
        return "visible" in self.classes


    def open(
        self,
        items: list[str],
        selected_idx: int,
        anchor_x: int,
        anchor_y: int,
    ) -> None:
        if not items:
            self.close()
            return
        self._items = list(items)
        self._selected = max(0, min(selected_idx, len(self._items) - 1))
        self._scroll = 0
        self._anchor_x = anchor_x
        self._anchor_y = anchor_y
        self._update_scroll()
        self._relayout()
        self.add_class("visible")
        self.refresh()


    def set_selection(self, selected_idx: int) -> None:
        if not self._items:
            return
        self._selected = max(0, min(selected_idx, len(self._items) - 1))
        self._update_scroll()
        self.refresh()


    def close(self) -> None:
        self.remove_class("visible")
        self._items = []
        self._selected = 0
        self._scroll = 0


    def _update_scroll(self) -> None:
        if not self._items:
            return
        rows = self._visible_rows()
        idx = self._selected
        if idx < self._scroll:
            self._scroll = idx
        elif idx >= self._scroll + rows:
            self._scroll = idx - rows + 1
        max_scroll = max(0, len(self._items) - rows)
        self._scroll = max(0, min(self._scroll, max_scroll))


    def _visible_rows(self) -> int:
        """Rows actually shown: every candidate, capped only by available
        space above the bar so the popup never spills off the screen top."""
        max_above = max(1, self._anchor_y)
        return max(1, min(len(self._items), max_above))


    def _relayout(self) -> None:
        max_item_w = max(len(item) for item in self._items)
        # +2 for one-cell padding on each side.
        screen_w = max(1, self.app.size.width)
        self._popup_w = min(max_item_w + 2, screen_w)
        self._popup_h = self._visible_rows()

        x = self._anchor_x
        if x + self._popup_w > screen_w:
            x = max(0, screen_w - self._popup_w)
        # Float upward so the popup's bottom edge sits one row above the bar.
        y = max(0, self._anchor_y - self._popup_h)

        self.styles.offset = (x, y)
        self.styles.width = self._popup_w
        self.styles.height = self._popup_h


    def render_line(self, y: int) -> Strip:
        item_rich = RichStyle.parse(self.hl.style("Pmenu"))
        sel_rich = RichStyle.parse(self.hl.style("PmenuSel"))
        rows = self._popup_h
        width = self._popup_w

        if y >= rows or not self._items:
            return Strip([Segment(" " * width, item_rich)], width)

        idx = self._scroll + y
        if idx >= len(self._items):
            return Strip([Segment(" " * width, item_rich)], width)

        item = self._items[idx]
        cell = " " + item
        if len(cell) < width:
            cell += " " * (width - len(cell))
        else:
            cell = cell[:width]

        if idx == self._selected:
            row_rich = sel_rich
        else:
            row_rich = item_rich
        return Strip([Segment(cell, row_rich)], width)
