"""
Simple cascading context menu overlay for tgdb.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from .highlight_groups import HighlightGroups


@dataclass(frozen=True)
class ContextMenuItem:
    label: str
    action: Optional[str] = None
    children: tuple["ContextMenuItem", ...] = ()
    separator_before: bool = False

    @property
    def has_children(self) -> bool:
        return bool(self.children)


@dataclass(frozen=True)
class _PanelRow:
    kind: str
    item_index: Optional[int] = None


@dataclass(frozen=True)
class _PanelLayout:
    items: tuple[ContextMenuItem, ...]
    selected_index: int
    x: int
    y: int
    inner_width: int
    rows: tuple[_PanelRow, ...]

    @property
    def width(self) -> int:
        return self.inner_width + 2

    @property
    def height(self) -> int:
        return len(self.rows) + 2

    def row_for_item(self, item_index: int) -> Optional[int]:
        for row_index, row in enumerate(self.rows):
            if row.kind == "item" and row.item_index == item_index:
                return row_index
        return None


class ContextMenu(Widget):
    """Small popup menu shown on right click."""

    DEFAULT_CSS = """
    ContextMenu {
        layer: dialog;
        position: absolute;
        display: none;
        background: transparent;
    }
    ContextMenu.visible {
        display: block;
    }
    """

    _PADDING_LEFT = 2
    _PADDING_RIGHT = 2
    _SUBMENU_GLYPH = "▸"

    def __init__(
        self,
        hl: HighlightGroups,
        items: Optional[Sequence[ContextMenuItem]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._items: tuple[ContextMenuItem, ...] = tuple(items or ())
        self._selection_path: list[int] = [0] if self._items else []
        self._requested_x = 0
        self._requested_y = 0
        self._panels: list[_PanelLayout] = []
        self._menu_width = 1
        self._menu_height = 1
        self.can_focus = True

    @property
    def is_open(self) -> bool:
        return self.has_class("visible")

    def set_items(self, items: Sequence[ContextMenuItem]) -> None:
        self._items = tuple(items)
        self._selection_path = [0] if self._items else []
        if self.is_open:
            self._relayout()
        else:
            self.refresh()

    def open_at(self, screen_x: int, screen_y: int) -> None:
        self._requested_x = screen_x
        self._requested_y = screen_y
        if self._items and not self._selection_path:
            self._selection_path = [0]
        self._relayout()
        self.add_class("visible")
        self.focus()
        self.refresh()

    def close(self) -> None:
        self.remove_class("visible")
        self.refresh()

    def contains_point(self, screen_x: int, screen_y: int) -> bool:
        if not self.is_open:
            return False
        local_x = screen_x - self.region.x
        local_y = screen_y - self.region.y
        return self._panel_bounds_at(local_x, local_y) is not None

    def _entries_at_depth(self, depth: int) -> tuple[ContextMenuItem, ...]:
        items = self._items
        for level in range(depth):
            if not items or level >= len(self._selection_path):
                return ()
            index = self._selection_path[level]
            if index < 0 or index >= len(items):
                return ()
            items = items[index].children
        return items

    def _selected_item(self, depth: Optional[int] = None) -> Optional[ContextMenuItem]:
        if not self._selection_path:
            return None
        if depth is None:
            depth = len(self._selection_path) - 1
        items = self._entries_at_depth(depth)
        if not items:
            return None
        index = self._selection_path[depth]
        if 0 <= index < len(items):
            return items[index]
        return None

    def _row_layout(self, items: tuple[ContextMenuItem, ...]) -> tuple[_PanelRow, ...]:
        rows: list[_PanelRow] = []
        for index, item in enumerate(items):
            if index and item.separator_before:
                rows.append(_PanelRow("separator"))
            rows.append(_PanelRow("item", item_index=index))
        return tuple(rows)

    def _inner_width(self, items: tuple[ContextMenuItem, ...]) -> int:
        base_padding = self._PADDING_LEFT + self._PADDING_RIGHT
        submenu_tail = 3
        return max(
            (
                cell_len(item.label) + base_padding + (submenu_tail if item.has_children else 0)
                for item in items
            ),
            default=1,
        )

    def _normalize_selection_path(self) -> None:
        if not self._items:
            self._selection_path = []
            return

        if not self._selection_path:
            self._selection_path = [0]

        normalized: list[int] = []
        items = self._items
        for depth, index in enumerate(self._selection_path):
            if not items:
                break
            clamped = max(0, min(len(items) - 1, index))
            normalized.append(clamped)
            item = items[clamped]
            if depth == len(self._selection_path) - 1 or not item.has_children:
                break
            items = item.children
        self._selection_path = normalized or [0]

    def _set_selection(
        self,
        depth: int,
        index: int,
        *,
        open_child: bool,
        preserve_child: bool = False,
    ) -> None:
        items = self._entries_at_depth(depth)
        if not items:
            return

        index = max(0, min(len(items) - 1, index))
        current_child = None
        if preserve_child and len(self._selection_path) > depth + 1:
            current_child = self._selection_path[depth + 1]

        self._selection_path = self._selection_path[: depth + 1]
        if len(self._selection_path) <= depth:
            self._selection_path.extend([0] * (depth + 1 - len(self._selection_path)))
        self._selection_path[depth] = index

        item = items[index]
        if open_child and item.has_children:
            child_index = 0
            if current_child is not None and item.children:
                child_index = max(0, min(len(item.children) - 1, current_child))
            self._selection_path.append(child_index)

        self._normalize_selection_path()
        self._relayout()

    def _open_child_panel(self, depth: int) -> bool:
        item = self._selected_item(depth)
        if item is None or not item.has_children:
            return False
        self._set_selection(
            depth,
            self._selection_path[depth],
            open_child=True,
            preserve_child=True,
        )
        return True

    def _close_child_panel(self) -> bool:
        if len(self._selection_path) <= 1:
            return False
        self._selection_path.pop()
        self._normalize_selection_path()
        self._relayout()
        return True

    def _relayout(self) -> None:
        self._normalize_selection_path()
        panels: list[_PanelLayout] = []
        x = 0
        y = 0
        for depth in range(len(self._selection_path)):
            items = self._entries_at_depth(depth)
            if not items:
                break
            selected_index = max(0, min(len(items) - 1, self._selection_path[depth]))
            panel = _PanelLayout(
                items=items,
                selected_index=selected_index,
                x=x,
                y=y,
                inner_width=self._inner_width(items),
                rows=self._row_layout(items),
            )
            panels.append(panel)
            selected_item = items[selected_index]
            if not selected_item.has_children or depth >= len(self._selection_path) - 1:
                break
            row_index = panel.row_for_item(selected_index)
            if row_index is None:
                break
            x = panel.x + panel.width
            y = panel.y + 1 + row_index

        self._panels = panels
        self._menu_width = max((panel.x + panel.width for panel in panels), default=1)
        self._menu_height = max((panel.y + panel.height for panel in panels), default=1)

        max_x = max(0, self.screen.size.width - self._menu_width)
        max_y = max(0, self.screen.size.height - self._menu_height)
        self.styles.width = self._menu_width
        self.styles.height = self._menu_height
        self.styles.offset = (
            max(0, min(max_x, self._requested_x)),
            max(0, min(max_y, self._requested_y)),
        )
        self.refresh()

    def _panel_bounds_at(self, local_x: int, local_y: int) -> Optional[int]:
        for depth in range(len(self._panels) - 1, -1, -1):
            panel = self._panels[depth]
            if (
                panel.x <= local_x < panel.x + panel.width
                and panel.y <= local_y < panel.y + panel.height
            ):
                return depth
        return None

    def _panel_item_at(self, local_x: int, local_y: int) -> Optional[tuple[int, int]]:
        for depth in range(len(self._panels) - 1, -1, -1):
            panel = self._panels[depth]
            if not (
                panel.x <= local_x < panel.x + panel.width
                and panel.y <= local_y < panel.y + panel.height
            ):
                continue
            inner_x = local_x - panel.x
            inner_y = local_y - panel.y
            if inner_x in (0, panel.width - 1) or inner_y in (0, panel.height - 1):
                return None
            row = panel.rows[inner_y - 1]
            if row.kind != "item" or row.item_index is None:
                return None
            return depth, row.item_index
        return None

    def _select_pointer(self, local_x: int, local_y: int) -> bool:
        hit = self._panel_item_at(local_x, local_y)
        if hit is None:
            return False
        depth, item_index = hit
        self._set_selection(depth, item_index, open_child=True)
        return True

    def _submit_selection(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if item.has_children:
            self._open_child_panel(len(self._selection_path) - 1)
            return
        if item.action:
            self.post_message(ContextMenuSelected(item.action))

    def _item_row_text(self, panel: _PanelLayout, item: ContextMenuItem) -> str:
        left = " " * self._PADDING_LEFT
        right = " " * self._PADDING_RIGHT
        if item.has_children:
            tail = f" {self._SUBMENU_GLYPH} "
            filler = max(1, panel.inner_width - cell_len(left) - cell_len(item.label) - cell_len(tail))
            return f"{left}{item.label}{' ' * filler}{tail}"
        filler = max(0, panel.inner_width - cell_len(left) - cell_len(item.label) - cell_len(right))
        return f"{left}{item.label}{' ' * filler}{right}"

    def _draw_panel_row(
        self,
        panel: _PanelLayout,
        panel_row: int,
        chars: list[str],
        styles_row: list,
        total_width: int,
        border_rich: "RichStyle",
        sel_rich: "RichStyle",
    ) -> None:
        """Fill one row of a panel into the chars/styles_row arrays."""
        inner_width = panel.inner_width

        if panel_row == 0:
            line = "┌" + ("─" * inner_width) + "┐"
            for dx, ch in enumerate(line):
                xi = panel.x + dx
                if 0 <= xi < total_width:
                    chars[xi] = ch
                    styles_row[xi] = border_rich
        elif panel_row == panel.height - 1:
            line = "└" + ("─" * inner_width) + "┘"
            for dx, ch in enumerate(line):
                xi = panel.x + dx
                if 0 <= xi < total_width:
                    chars[xi] = ch
                    styles_row[xi] = border_rich
        else:
            row_idx = panel_row - 1
            if row_idx >= len(panel.rows):
                return
            row = panel.rows[row_idx]
            if row.kind == "separator":
                line = "├" + ("─" * inner_width) + "┤"
                for dx, ch in enumerate(line):
                    xi = panel.x + dx
                    if 0 <= xi < total_width:
                        chars[xi] = ch
                        styles_row[xi] = border_rich
            else:
                assert row.item_index is not None
                item = panel.items[row.item_index]
                row_rich = sel_rich if row.item_index == panel.selected_index else border_rich
                xi = panel.x
                if 0 <= xi < total_width:
                    chars[xi] = "│"
                    styles_row[xi] = border_rich
                inner_text = self._item_row_text(panel, item)
                for dx, ch in enumerate(inner_text, start=1):
                    xi = panel.x + dx
                    if 0 <= xi < total_width:
                        chars[xi] = ch
                        styles_row[xi] = row_rich
                xi = panel.x + panel.width - 1
                if 0 <= xi < total_width:
                    chars[xi] = "│"
                    styles_row[xi] = border_rich

    def _draw_panel(
        self,
        panel: _PanelLayout,
        chars: list[list[str]],
        styles: list[list[Optional[str]]],
    ) -> None:
        border_style = self.hl.style("StatusLine")
        top = "┌" + ("─" * panel.inner_width) + "┐"
        bottom = "└" + ("─" * panel.inner_width) + "┘"
        separator = "├" + ("─" * panel.inner_width) + "┤"

        for dx, ch in enumerate(top):
            chars[panel.y][panel.x + dx] = ch
            styles[panel.y][panel.x + dx] = border_style

        for row_offset, row in enumerate(panel.rows, start=1):
            y = panel.y + row_offset
            if row.kind == "separator":
                line = separator
                row_style = border_style
                for dx, ch in enumerate(line):
                    chars[y][panel.x + dx] = ch
                    styles[y][panel.x + dx] = row_style
                continue

            assert row.item_index is not None
            item = panel.items[row.item_index]
            row_style = (
                self.hl.style("SelectedLineHighlight")
                if row.item_index == panel.selected_index
                else self.hl.style("StatusLine")
            )
            chars[y][panel.x] = "│"
            styles[y][panel.x] = border_style
            inner = self._item_row_text(panel, item)
            for dx, ch in enumerate(inner, start=1):
                chars[y][panel.x + dx] = ch
                styles[y][panel.x + dx] = row_style
            chars[y][panel.x + panel.width - 1] = "│"
            styles[y][panel.x + panel.width - 1] = border_style

        bottom_y = panel.y + panel.height - 1
        for dx, ch in enumerate(bottom):
            chars[bottom_y][panel.x + dx] = ch
            styles[bottom_y][panel.x + dx] = border_style

    def render(self) -> Text:
        width = max(1, self._menu_width)
        height = max(1, self._menu_height)
        chars = [[" "] * width for _ in range(height)]
        styles: list[list[Optional[str]]] = [[None] * width for _ in range(height)]

        for panel in self._panels:
            self._draw_panel(panel, chars, styles)

        result = Text(no_wrap=True, overflow="crop")
        for y in range(height):
            if y:
                result.append("\n")
            for x in range(width):
                result.append(chars[y][x], style=styles[y][x])
        return result

    def render_line(self, y: int) -> Strip:
        """Render one line as a Strip.

        Cells outside any panel use Style.null() (no background) so Textual's
        layer compositor shows whatever is on the base layer beneath them —
        e.g. the status bar's gray background.  Using render() → Text would
        cause those cells to inherit the widget's visual_style background
        (resolved from 'background: transparent' → dark), covering content
        behind the menu widget's bounding box.
        """
        width  = max(1, self._menu_width)
        height = max(1, self._menu_height)
        if y >= height:
            return Strip([Segment(" " * width, RichStyle.null())], width)

        chars: list[str] = [" "] * width
        # styles_row holds RichStyle objects (not strings); None = transparent.
        # Parse hl.style() strings to RichStyle here once per render_line call.
        border_rich  = RichStyle.parse(self.hl.style("StatusLine"))
        sel_rich     = RichStyle.parse(self.hl.style("SelectedLineHighlight"))
        styles_row: list[Optional[RichStyle]] = [None] * width

        for panel in self._panels:
            panel_row = y - panel.y
            if 0 <= panel_row < panel.height:
                self._draw_panel_row(
                    panel, panel_row, chars, styles_row, width,
                    border_rich, sel_rich,
                )

        segments: list[Segment] = []
        for x in range(width):
            s = styles_row[x]
            if s is None:
                # Outside every panel: transparent — show the base layer
                segments.append(Segment(" ", RichStyle.null()))
            else:
                segments.append(Segment(chars[x], s))
        return Strip(segments, width)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.is_open and self._select_pointer(int(event.x), int(event.y)):
            event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self.is_open or event.button != 1:
            return
        panel_depth = self._panel_bounds_at(int(event.x), int(event.y))
        if panel_depth is None:
            self.post_message(ContextMenuClosed())
            event.stop()
            return
        hit = self._panel_item_at(int(event.x), int(event.y))
        if hit is None:
            event.stop()
            return
        depth, item_index = hit
        item = self._entries_at_depth(depth)[item_index]
        self._set_selection(depth, item_index, open_child=item.has_children)
        if item.has_children:
            event.stop()
            return
        self._submit_selection()
        event.stop()

    def on_key(self, event: events.Key) -> None:
        if not self.is_open or not self._selection_path:
            return

        depth = len(self._selection_path) - 1
        items = self._entries_at_depth(depth)
        if not items:
            return

        key = event.key
        char = event.character or ""
        if key == "up" or char == "k":
            self._set_selection(depth, self._selection_path[depth] - 1, open_child=False)
        elif key == "down" or char == "j":
            self._set_selection(depth, self._selection_path[depth] + 1, open_child=False)
        elif key == "right" or char == "l":
            self._open_child_panel(depth)
        elif key == "left" or char == "h":
            self._close_child_panel()
        elif key in ("enter", "return"):
            self._submit_selection()
        elif key == "escape" or char == "q":
            self.post_message(ContextMenuClosed())
        else:
            return
        event.stop()


class ContextMenuSelected(Message):
    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.item = action


class ContextMenuClosed(Message):
    pass
