"""
Simple context menu overlay for tgdb.
"""
from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widget import Widget
from rich.cells import cell_len
from rich.text import Text

from .highlight_groups import HighlightGroups


class ContextMenu(Widget):
    """Small popup menu shown on right click."""

    DEFAULT_CSS = """
    ContextMenu {
        layer: dialog;
        position: absolute;
        display: none;
        background: $surface;
    }
    ContextMenu.visible {
        display: block;
    }
    """

    def __init__(self, hl: HighlightGroups, items: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._items = list(items)
        self._sel = 0
        self.can_focus = True

    @property
    def is_open(self) -> bool:
        return self.has_class("visible")

    def open_at(self, screen_x: int, screen_y: int) -> None:
        menu_width = self._menu_width()
        menu_height = self._menu_height()
        max_x = max(0, self.screen.size.width - menu_width)
        max_y = max(0, self.screen.size.height - menu_height)
        self._sel = 0
        self.styles.width = menu_width
        self.styles.height = menu_height
        self.styles.offset = (
            max(0, min(max_x, screen_x)),
            max(0, min(max_y, screen_y)),
        )
        self.add_class("visible")
        self.focus()
        self.refresh()

    def close(self) -> None:
        self.remove_class("visible")
        self.refresh()

    def _menu_width(self) -> int:
        return max((cell_len(item) for item in self._items), default=0) + 2

    def _menu_height(self) -> int:
        return max(1, len(self._items))

    def _select_index(self, index: int) -> None:
        if not self._items:
            return
        self._sel = max(0, min(len(self._items) - 1, index))
        self.refresh()

    def _select_pointer_row(self, y: int) -> bool:
        if 0 <= y < len(self._items):
            self._select_index(y)
            return True
        return False

    def _submit_selection(self) -> None:
        if 0 <= self._sel < len(self._items):
            self.post_message(ContextMenuSelected(self._items[self._sel]))

    def render(self) -> Text:
        width = max(1, self._menu_width())
        result = Text(no_wrap=True, overflow="crop")
        for index, item in enumerate(self._items):
            if index:
                result.append("\n")
            style = (
                self.hl.style("SelectedLineHighlight")
                if index == self._sel
                else self.hl.style("StatusLine")
            )
            line = f" {item}"
            result.append(line + (" " * max(0, width - cell_len(line))), style=style)
        return result

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.is_open and self._select_pointer_row(int(event.y)):
            event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self.is_open:
            return
        if event.button == 1 and self._select_pointer_row(int(event.y)):
            self._submit_selection()
            event.stop()

    def on_key(self, event: events.Key) -> None:
        if not self.is_open:
            return

        key = event.key
        char = event.character or ""
        if key in ("up",) or char == "k":
            self._select_index(self._sel - 1)
        elif key in ("down",) or char == "j":
            self._select_index(self._sel + 1)
        elif key in ("enter", "return"):
            self._submit_selection()
        elif key == "escape" or char == "q":
            self.post_message(ContextMenuClosed())
        else:
            return
        event.stop()


class ContextMenuSelected(Message):
    def __init__(self, item: str) -> None:
        super().__init__()
        self.item = item


class ContextMenuClosed(Message):
    pass
