"""Panel renderer used by the cascading context menu."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich.segment import Segment
from rich.style import Style as RichStyle
from textual import events
from textual.strip import Strip
from textual.widget import Widget

from .context_menu_model import _PanelLayout, _item_row_text
from .highlight_groups import HighlightGroups

if TYPE_CHECKING:
    from .context_menu import ContextMenu


class _PanelWidget(Widget):
    """Render one panel box in the cascading context menu."""

    DEFAULT_CSS = """
    _PanelWidget {
        layer: dialog;
        position: absolute;
    }
    """

    def __init__(
        self,
        hl: HighlightGroups,
        panel: _PanelLayout,
        menu: "ContextMenu",
        depth: int,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._hl = hl
        self._panel = panel
        self._menu = menu
        self._depth = depth
        self.can_focus = False


    def update(self, panel: _PanelLayout, depth: int, abs_x: int, abs_y: int) -> None:
        self._panel = panel
        self._depth = depth
        self.styles.offset = (abs_x, abs_y)
        self.styles.width = panel.width
        self.styles.height = panel.height
        self.refresh()


    def render_line(self, y: int) -> Strip:
        panel = self._panel
        width = panel.width
        inner_width = panel.inner_width
        border_rich = RichStyle.parse(self._hl.style("StatusLine"))
        selected_rich = RichStyle.parse(self._hl.style("SelectedLineHighlight"))

        if y == 0:
            line = "┌" + "─" * inner_width + "┐"
            return Strip([Segment(ch, border_rich) for ch in line], width)

        if y == panel.height - 1:
            line = "└" + "─" * inner_width + "┘"
            return Strip([Segment(ch, border_rich) for ch in line], width)

        row_index = y - 1
        if row_index >= len(panel.rows):
            return Strip([Segment(" ", border_rich)] * width, width)

        row = panel.rows[row_index]
        if row.kind == "separator":
            line = "├" + "─" * inner_width + "┤"
            return Strip([Segment(ch, border_rich) for ch in line], width)

        assert row.item_index is not None
        item = panel.items[row.item_index]
        row_rich = border_rich
        if row.item_index == panel.selected_index:
            row_rich = selected_rich
        inner = _item_row_text(panel, item)
        segments = [Segment("│", border_rich)]
        segments.extend(Segment(ch, row_rich) for ch in inner)
        segments.append(Segment("│", border_rich))
        return Strip(segments, width)


    def _item_at(self, lx: int, ly: int) -> Optional[tuple[int, int]]:
        panel = self._panel
        if not (1 <= lx < panel.width - 1 and 1 <= ly < panel.height - 1):
            return None
        row_index = ly - 1
        if row_index >= len(panel.rows):
            return None
        row = panel.rows[row_index]
        if row.kind != "item" or row.item_index is None:
            return None
        return self._depth, row.item_index


    def on_mouse_move(self, event: events.MouseMove) -> None:
        hit = self._item_at(int(event.x), int(event.y))
        if hit is not None:
            self._menu._handle_panel_hover(hit[0], hit[1])
        event.stop()


    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        hit = self._item_at(int(event.x), int(event.y))
        if hit is not None:
            self._menu._handle_panel_click(hit[0], hit[1])
        event.stop()
