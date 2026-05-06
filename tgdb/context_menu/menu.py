"""
Public implementation of the cascading context-menu package.

Each visible panel is rendered by its own ``_PanelWidget``, sized to exactly
match that panel's rectangle. This means there are no empty cells between
panels, so underlying content remains visible through the gaps.
"""

from collections.abc import Sequence

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from .model import (
    ContextMenuItem,
    _PADDING_LEFT,
    _PADDING_RIGHT,
    _PanelLayout,
    _PanelRow,
    _SUBMENU_GLYPH,
)
from .panel import _PanelWidget
from ..highlight_groups import HighlightGroups

class ContextMenu(Widget):
    """Cascading popup context menu used by tgdb's workspace.

    Public interface
    ----------------
    ``ContextMenu(hl, items=None, **kwargs)``
        Create the menu widget.

    ``set_items(items)``
        Replace the root menu items.

    ``open_at(x, y)``, ``close()``
        Show or hide the popup at screen coordinates.

    ``is_open`` and ``contains_point(x, y)``
        Query the visible state and geometry from the outside.

    Callers should treat the widget as a black box. Once items are supplied, it
    owns cascading-panel layout, focus, mouse/keyboard navigation, and emits
    ``ContextMenuSelected`` / ``ContextMenuClosed`` when user interaction
    completes.
    """

    DEFAULT_CSS = """
    ContextMenu {
        layer: dialog;
        position: absolute;
        width: 1;
        height: 1;
        display: none;
        background: transparent;
    }
    ContextMenu.visible {
        display: block;
    }
    """

    _PADDING_LEFT = _PADDING_LEFT
    _PADDING_RIGHT = _PADDING_RIGHT
    _SUBMENU_GLYPH = _SUBMENU_GLYPH

    def __init__(
        self,
        hl: HighlightGroups,
        items: Sequence[ContextMenuItem] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._items: tuple[ContextMenuItem, ...] = tuple(items or ())
        if self._items:
            self._selection_path: list[int] = [0]
        else:
            self._selection_path: list[int] = []
        self._requested_x = 0
        self._requested_y = 0
        self._panels: list[_PanelLayout] = []
        self._panel_widgets: list[_PanelWidget] = []
        self.can_focus = False


    def render(self) -> Text:
        # The ContextMenu widget itself is 1×1 and hidden under the root panel.
        return Text()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.has_class("visible")


    def set_items(self, items: Sequence[ContextMenuItem]) -> None:
        self._items = tuple(items)
        if self._items:
            self._selection_path = [0]
        else:
            self._selection_path = []
        if self.is_open:
            self._relayout()


    def open_at(self, screen_x: int, screen_y: int) -> None:
        self._requested_x = screen_x
        self._requested_y = screen_y
        if self._items and not self._selection_path:
            self._selection_path = [0]
        self._relayout()
        self.add_class("visible")


    def close(self) -> None:
        self.remove_class("visible")
        for pw in self._panel_widgets:
            pw.remove()
        self._panel_widgets = []


    def contains_point(self, screen_x: int, screen_y: int) -> bool:
        if not self.is_open:
            return False
        for pw in self._panel_widgets:
            r = pw.region
            if r.x <= screen_x < r.x + r.width and r.y <= screen_y < r.y + r.height:
                return True
        return False

    # ------------------------------------------------------------------
    # Forwarded from _PanelWidget
    # ------------------------------------------------------------------

    def _handle_panel_hover(self, depth: int, item_index: int) -> None:
        # Mouse motion fires this on every event over the panel.  Bail
        # whenever the hovered (depth, item_index) already matches the
        # current selection at that depth — even when an open child
        # panel exists at a deeper depth.  The previous guard required
        # ``depth == len(self._selection_path) - 1``, which meant
        # mousing back over a parent panel item that was already
        # selected (with its child panel still showing) re-ran
        # ``_set_selection``: that truncates ``_selection_path`` and
        # reopens the child from index 0, triggering a full
        # ``_relayout`` and visibly resetting the user's submenu cursor
        # on every passing mouse motion.
        if (
            depth < len(self._selection_path)
            and self._selection_path[depth] == item_index
        ):
            return
        self._set_selection(depth, item_index, open_child=True)


    def _handle_panel_click(self, depth: int, item_index: int) -> None:
        items = self._entries_at_depth(depth)
        if not items or item_index >= len(items):
            return
        item = items[item_index]
        self._set_selection(depth, item_index, open_child=item.has_children)
        if not item.has_children:
            self._submit_selection()

    # ------------------------------------------------------------------
    # Internal state helpers
    # ------------------------------------------------------------------

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


    def _selected_item(self, depth: int | None = None) -> ContextMenuItem | None:
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
        base_padding = _PADDING_LEFT + _PADDING_RIGHT
        submenu_tail = 3
        widths = []
        for item in items:
            if item.has_children:
                extra = submenu_tail
            else:
                extra = 0
            widths.append(cell_len(item.label) + base_padding + extra)
        return max(widths, default=1)


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

        # Recompute panel layouts
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

        # Clamp the whole cascade to stay on screen
        total_w = max((p.x + p.width for p in panels), default=1)
        total_h = max((p.y + p.height for p in panels), default=1)
        scr_w = self.screen.size.width
        scr_h = self.screen.size.height
        origin_x = max(0, min(scr_w - total_w, self._requested_x))
        origin_y = max(0, min(scr_h - total_h, self._requested_y))

        # Park the ContextMenu widget (1×1) at the top-left of the cascade
        # so it stays focusable and its position is predictable.
        self.styles.offset = (origin_x, origin_y)

        # Update or create _PanelWidget instances (reuse to avoid flicker)
        for i, panel in enumerate(panels):
            abs_x = origin_x + panel.x
            abs_y = origin_y + panel.y
            if i < len(self._panel_widgets):
                self._panel_widgets[i].update(panel, i, abs_x, abs_y)
            else:
                pw = _PanelWidget(self.hl, panel, self, i)
                pw.styles.offset = (abs_x, abs_y)
                pw.styles.width = panel.width
                pw.styles.height = panel.height
                self.app.screen.mount(pw)
                self._panel_widgets.append(pw)

        # Remove excess widgets (submenus that are now closed)
        while len(self._panel_widgets) > len(panels):
            self._panel_widgets.pop().remove()


    def _submit_selection(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if item.has_children:
            self._open_child_panel(len(self._selection_path) - 1)
            return
        if item.action:
            self.post_message(ContextMenuSelected(item.action))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_unmount(self) -> None:
        for pw in self._panel_widgets:
            pw.remove()
        self._panel_widgets = []

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

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
            self._set_selection(
                depth, self._selection_path[depth] - 1, open_child=False
            )
        elif key == "down" or char == "j":
            self._set_selection(
                depth, self._selection_path[depth] + 1, open_child=False
            )
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


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class ContextMenuSelected(Message):
    """Publish the action chosen from the context menu."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action


class ContextMenuClosed(Message):
    """Request that the context menu close without taking an action."""

    pass
