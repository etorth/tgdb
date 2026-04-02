"""
Workspace/layout helper widgets for tgdb.

These classes were split out of ``app.py`` so the main application module can
focus on orchestration while the reusable workspace layout widgets live in
their own module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from .highlight_groups import HighlightGroups
from .pane_base import PaneBase  # noqa: F401 — re-exported for callers


class DragResize(Message):
    """Resize the split by dragging the splitter widget."""

    def __init__(
        self,
        screen_x: int = 0,
        screen_y: int = 0,
        splitter: Optional["Splitter"] = None,
    ) -> None:
        super().__init__()
        self.screen_x = screen_x
        self.screen_y = screen_y
        self.splitter = splitter


class TitleBarResized(Message):
    """Posted after a title-bar drag resizes two items in a vertical PaneContainer.

    ``new_before_size`` is the resulting pixel height of the *before* pane so
    that the app can sync its internal ``_window_shift`` state.
    """

    def __init__(self, container: "PaneContainer", new_before_size: int) -> None:
        super().__init__()
        self.container = container
        self.new_before_size = new_before_size


@dataclass(frozen=True)
class PaneDescriptor:
    label: str
    create: Callable[[], Widget]
    current: Callable[[], Optional[Widget]]
    requester: Optional[Callable[[], None]] = None


class Splitter(Widget):
    """1-cell splitter between the source and GDB panes."""

    DEFAULT_CSS = """
    Splitter {
        background: $primary-darken-2;
    }
    """

    def __init__(self, hl: HighlightGroups, draggable: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._draggable = draggable
        self._dragging = False
        self._is_horizontal_split = True

    def set_orientation(self, is_horizontal_split: bool) -> None:
        self._is_horizontal_split = is_horizontal_split
        if is_horizontal_split:
            self.styles.width = 1
            self.styles.height = "1fr"
        else:
            self.styles.width = "1fr"
            self.styles.height = 1
        self.refresh()

    def render(self) -> Text:
        style = self.hl.style("StatusLine")
        if self._is_horizontal_split:
            height = max(1, self.size.height or 1)
            return Text(
                "\n".join(" " for _ in range(height)),
                style=style,
                no_wrap=True,
                overflow="crop",
            )
        width = max(1, self.size.width or 1)
        return Text(" " * width, style=style, no_wrap=True, overflow="crop")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self._draggable and event.button == 1:
            self._dragging = True
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            self.post_message(
                DragResize(
                    screen_x=int(event.screen_x),
                    screen_y=int(event.screen_y),
                    splitter=self,
                )
            )
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging and event.button == 1:
            self._dragging = False
            self.release_mouse()
            event.stop()


class EmptyPane(PaneBase):
    """An empty workspace leaf created by context-menu split actions."""

    DEFAULT_CSS = """
    EmptyPane {
        width: 1fr;
        height: 1fr;
        min-width: 4;
        min-height: 2;
        background: $surface-darken-1;
    }
    """


class PaneContainer(Widget):
    """A generic horizontal/vertical container with resizable child items."""

    DEFAULT_CSS = """
    PaneContainer {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(
        self,
        hl: HighlightGroups,
        orientation: str = "horizontal",
        min_item_width: int = 4,
        min_item_height: int = 2,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.orientation = orientation
        self._items: list[Widget] = []
        self._weights: list[int] = []
        self.min_item_width = min_item_width
        self.min_item_height = min_item_height

    @property
    def items(self) -> tuple[Widget, ...]:
        return tuple(self._items)

    def index_of(self, item: Widget) -> int:
        return self._items.index(item)

    def set_orientation(self, orientation: str) -> None:
        if self.orientation == orientation:
            return
        self.orientation = orientation
        # _rebuild is async and correctly adds/removes Splitter widgets.
        # Schedule it for the next frame; callers can proceed synchronously.
        self.call_later(self._rebuild)

    async def set_items(self, items: list[Widget]) -> None:
        self._items = list(items)
        self._weights = [1] * len(self._items)
        await self._rebuild()

    async def insert_item(self, index: int, item: Widget) -> None:
        self._items.insert(index, item)
        self._weights = [1] * len(self._items)
        await self._rebuild()

    async def replace_item(self, old_item: Widget, new_item: Widget) -> None:
        index = self.index_of(old_item)
        self._items[index] = new_item
        if len(self._weights) != len(self._items):
            self._weights = [1] * len(self._items)
        await self._rebuild()

    async def take_item(self, item: Widget) -> Widget:
        index = self.index_of(item)
        removed = self._items.pop(index)
        if len(self._weights) == len(self._items) + 1:
            self._weights.pop(index)
        else:
            self._weights = [1] * len(self._items)
        await self._rebuild()
        return removed

    def _apply_item_style(self, item: Widget, weight: int) -> None:
        item.styles.display = "block"
        weight_fr = f"{max(1, int(weight))}fr"
        if self.orientation == "horizontal":
            item.styles.width = weight_fr
            item.styles.height = "1fr"
        else:
            item.styles.width = "1fr"
            item.styles.height = weight_fr

    def _apply_orientation(self) -> None:
        self.styles.layout = self.orientation
        is_horizontal = self.orientation == "horizontal"
        item_iter = iter(zip(self._items, self._weights or ([1] * len(self._items)), strict=False))
        for child in self.children:
            if isinstance(child, Splitter):
                child.set_orientation(is_horizontal)
            else:
                item, weight = next(item_iter, (child, 1))
                self._apply_item_style(item, weight)

    def _adjacent_items(self, splitter: "Splitter") -> Optional[tuple[Widget, Widget]]:
        children = list(self.children)
        try:
            index = children.index(splitter)
        except ValueError:
            return None
        if index <= 0 or index >= len(children) - 1:
            return None
        before = children[index - 1]
        after = children[index + 1]
        if isinstance(before, Splitter) or isinstance(after, Splitter):
            return None
        return before, after

    def _capture_layout_weights(self) -> None:
        is_horizontal = self.orientation == "horizontal"
        if not self._items:
            self._weights = []
            return
        self._weights = [max(1, item.size.width if is_horizontal else item.size.height) for item in self._items]

    def _resize_from_drag(self, splitter: "Splitter", screen_x: int, screen_y: int) -> bool:
        adjacent = self._adjacent_items(splitter)
        if adjacent is None:
            return False

        self._capture_layout_weights()
        before, after = adjacent
        before_index = self.index_of(before)
        after_index = self.index_of(after)
        is_horizontal = self.orientation == "horizontal"
        min_size = self.min_item_width if is_horizontal else self.min_item_height

        start = before.region.x if is_horizontal else before.region.y
        before_size = before.size.width if is_horizontal else before.size.height
        after_size = after.size.width if is_horizontal else after.size.height
        total_size = before_size + after_size
        if total_size <= (min_size * 2):
            return False

        pointer = screen_x if is_horizontal else screen_y
        new_before = max(min_size, min(total_size - min_size, int(pointer - start)))
        new_after = total_size - new_before
        if new_before <= 0 or new_after <= 0:
            return False

        self._weights[before_index] = new_before
        self._weights[after_index] = new_after
        self._apply_orientation()
        self.refresh(layout=True)
        return True

    async def _restore_nested_containers(self) -> None:
        """
        Rebuild nested PaneContainer children after this container is remounted.

        Textual unmounts the full subtree when a parent removes and re-mounts a
        child widget. For nested PaneContainer children, that means the child
        widget keeps its logical ``_items`` / ``_weights`` state but loses its
        mounted child widgets. Re-running the child's own rebuild restores its
        splitters and nested panes while preserving its stored weights.
        """
        for item in self._items:
            if not isinstance(item, PaneContainer):
                continue
            if item._items and not list(item.children):
                await item._rebuild()
            else:
                await item._restore_nested_containers()

    async def _rebuild(self) -> None:
        is_horizontal = self.orientation == "horizontal"
        if len(self._weights) != len(self._items):
            self._weights = [1] * len(self._items)
        children: list[Widget] = []
        for index, item in enumerate(self._items):
            self._apply_item_style(item, self._weights[index])
            children.append(item)
            if index < len(self._items) - 1:
                # In vertical containers the title bar of the next PaneBase
                # item acts as the visual/drag boundary — no separate Splitter.
                if is_horizontal:
                    splitter = Splitter(self.hl, draggable=True)
                    splitter.set_orientation(is_horizontal)
                    children.append(splitter)

        async with self.batch():
            await self.remove_children()
            self.styles.layout = self.orientation
            if children:
                # Textual's _mounted_event is a cached_property asyncio.Event
                # that is never reset after a widget is pruned.  When a widget
                # that was previously mounted (and pruned) is remounted, the
                # event is already set, so AwaitMount resolves immediately
                # without waiting for the widget's new _pre_process (compose +
                # mount) to run.  The widget ends up with no children for
                # several event-loop cycles, causing a blank/"stuck" pane.
                #
                # Reset the event for any child that has been mounted before
                # (detected by presence of '_mounted_event' in __dict__) so
                # AwaitMount correctly waits for each widget's compose cycle.
                for child in children:
                    if not isinstance(child, Splitter) and "_mounted_event" in child.__dict__:
                        child.__dict__["_mounted_event"] = asyncio.Event()
                await self.mount_all(children)
        await self._restore_nested_containers()
        self.refresh(layout=True)

    def on_drag_resize(self, msg: DragResize) -> None:
        splitter = msg.splitter
        if splitter is None or splitter.parent is not self:
            return
        # Always handle the drag; do NOT stop the message so it can bubble to
        # the app for top-level _window_shift bookkeeping.
        self._resize_from_drag(splitter, msg.screen_x, msg.screen_y)

    def _resize_from_title_drag(self, before: Widget, after: Widget, screen_y: int) -> None:
        """Resize *before* and *after* panes in a vertical container when the
        user drags the title bar of *after* (which is the visual boundary)."""
        self._capture_layout_weights()
        try:
            before_index = self._items.index(before)
            after_index = self._items.index(after)
        except ValueError:
            return

        before_size = before.size.height
        after_size = after.size.height
        total_size = before_size + after_size
        min_size = self.min_item_height

        if total_size <= min_size * 2:
            return

        start = before.region.y
        new_before = max(min_size, min(total_size - min_size, int(screen_y - start)))
        new_after = total_size - new_before

        if new_before <= 0 or new_after <= 0:
            return

        self._weights[before_index] = new_before
        self._weights[after_index] = new_after
        self._apply_orientation()
        self.refresh(layout=True)
        # Notify the app so it can sync _window_shift for proper on_resize behaviour.
        self.post_message(TitleBarResized(self, new_before))
