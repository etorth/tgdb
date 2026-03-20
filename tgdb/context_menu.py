"""
Simple cascading context menu overlay for tgdb.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from .highlight_groups import HighlightGroups


@dataclass(frozen=True)
class ContextMenuItem:
    label: str
    action: Optional[str] = None
    children: tuple["ContextMenuItem", ...] = ()

    @property
    def has_children(self) -> bool:
        return bool(self.children)


@dataclass(frozen=True)
class _PanelLayout:
    items: tuple[ContextMenuItem, ...]
    selected_index: int
    x: int
    y: int
    width: int

    @property
    def height(self) -> int:
        return len(self.items)


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
        return self._panel_at(local_x, local_y) is not None

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

    def _panel_width(self, items: tuple[ContextMenuItem, ...]) -> int:
        return max(
            (
                cell_len(f" {item.label}") + (2 if item.has_children else 0)
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
        self._set_selection(depth, self._selection_path[depth], open_child=True, preserve_child=True)
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
            width = self._panel_width(items)
            panels.append(
                _PanelLayout(
                    items=items,
                    selected_index=selected_index,
                    x=x,
                    y=y,
                    width=width,
                )
            )
            selected_item = items[selected_index]
            if not selected_item.has_children or depth >= len(self._selection_path) - 1:
                break
            x += width
            y += selected_index

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

    def _panel_row_text(self, panel: _PanelLayout, row_index: int) -> str:
        item = panel.items[row_index]
        base = f" {item.label}"
        if item.has_children:
            padding = max(1, panel.width - cell_len(base) - 1)
            return f"{base}{' ' * padding}›"
        return base + (" " * max(0, panel.width - cell_len(base)))

    def _panel_at(self, local_x: int, local_y: int) -> Optional[tuple[int, int]]:
        for depth in range(len(self._panels) - 1, -1, -1):
            panel = self._panels[depth]
            if (
                panel.x <= local_x < panel.x + panel.width
                and panel.y <= local_y < panel.y + panel.height
            ):
                return depth, local_y - panel.y
        return None

    def _select_pointer(self, local_x: int, local_y: int) -> bool:
        hit = self._panel_at(local_x, local_y)
        if hit is None:
            return False
        depth, row = hit
        self._set_selection(depth, row, open_child=True)
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

    def render(self) -> Text:
        width = max(1, self._menu_width)
        height = max(1, self._menu_height)
        result = Text(no_wrap=True, overflow="crop")

        for row in range(height):
            if row:
                result.append("\n")
            cursor_x = 0
            row_panels = [panel for panel in self._panels if panel.y <= row < panel.y + panel.height]
            row_panels.sort(key=lambda panel: panel.x)
            for depth, panel in ((self._panels.index(panel), panel) for panel in row_panels):
                if panel.x > cursor_x:
                    result.append(" " * (panel.x - cursor_x))
                    cursor_x = panel.x
                row_index = row - panel.y
                style = (
                    self.hl.style("SelectedLineHighlight")
                    if row_index == panel.selected_index
                    else self.hl.style("StatusLine")
                )
                result.append(self._panel_row_text(panel, row_index), style=style)
                cursor_x = panel.x + panel.width
            if cursor_x < width:
                result.append(" " * (width - cursor_x))
        return result

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.is_open and self._select_pointer(int(event.x), int(event.y)):
            event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self.is_open or event.button != 1:
            return
        hit = self._panel_at(int(event.x), int(event.y))
        if hit is None:
            self.post_message(ContextMenuClosed())
            event.stop()
            return
        depth, row = hit
        item = self._entries_at_depth(depth)[row]
        self._set_selection(depth, row, open_child=item.has_children)
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
