"""
Main Textual application — mirrors cgdb's interface.cpp + cgdb.cpp.

Global layout:
  ┌──────────────────────────┐
  │    Source / GDB area     │
  ├──────────────────────────┤
  │    dedicated status bar  │  1 line
  └──────────────────────────┘

The source pane itself reserves its bottom row for the current file path.

Modes: CGDB | GDB | SCROLL | STATUS | FILEDLG
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual import events
from textual.css.query import NoMatches
from rich.text import Text

from .highlight_groups import HighlightGroups
from .key_mapper import KeyMapper
from .config import Config, ConfigParser
from .gdb_controller import (
    GDBController,
    Breakpoint,
    Frame,
    LocalVariable,
    RegisterInfo,
    ThreadInfo,
)
from .source_widget import (
    SourceView, SourceFile,
    ToggleBreakpoint, OpenFileDialog, AwaitMarkJump, AwaitMarkSet,
    JumpGlobalMark, SearchStart, SearchUpdate, SearchCommit, SearchCancel,
    StatusMessage, ResizeSource, ToggleOrientation, OpenTTY, GDBCommand, ShowHelp,
)
from .gdb_widget import (
    GDBWidget, ScrollModeChange,
    ScrollSearchStart, ScrollSearchUpdate, ScrollSearchCommit, ScrollSearchCancel,
)
from .status_bar import StatusBar, CommandSubmit, CommandCancel
from .file_dialog import FileDialog, FileSelected, FileDialogClosed
from .context_menu import (
    ContextMenu,
    ContextMenuClosed,
    ContextMenuItem,
    ContextMenuSelected,
)
from .local_variable_pane import LocalVariablePane
from .register_pane import RegisterPane
from .stack_pane import StackPane
from .thread_pane import ThreadPane


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


class EmptyPane(Widget):
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

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.can_focus = True

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        return Text(" " * width, no_wrap=True, overflow="crop")


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
        self.orientation = orientation
        self._apply_orientation()
        self.refresh(layout=True)

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
        item_iter = iter(
            zip(self._items, self._weights or ([1] * len(self._items)), strict=False)
        )
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
        self._weights = [
            max(1, item.size.width if is_horizontal else item.size.height)
            for item in self._items
        ]

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

    async def _rebuild(self) -> None:
        is_horizontal = self.orientation == "horizontal"
        if len(self._weights) != len(self._items):
            self._weights = [1] * len(self._items)
        children: list[Widget] = []
        for index, item in enumerate(self._items):
            self._apply_item_style(item, self._weights[index])
            children.append(item)
            if index < len(self._items) - 1:
                splitter = Splitter(self.hl, draggable=True)
                splitter.set_orientation(is_horizontal)
                children.append(splitter)

        async with self.batch():
            await self.remove_children()
            self.styles.layout = self.orientation
            if children:
                await self.mount_all(children)
        self.refresh(layout=True)

    def on_drag_resize(self, msg: DragResize) -> None:
        splitter = msg.splitter
        if splitter is None or splitter.parent is not self:
            return
        if self._resize_from_drag(splitter, msg.screen_x, msg.screen_y):
            msg.stop()


class TGDBApp(App):
    """tgdb — Python front-end for GDB, compatible with cgdb."""

    CSS = """
    Screen {
        layers: base dialog;
        layout: vertical;
    }
    #global-container {
        layer: base;
        layout: vertical;
        height: 1fr;
        width: 1fr;
    }
    #split-container {
        layout: horizontal;
        height: 1fr;
        width: 1fr;
    }
    #src-pane {
        width: 1fr;
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #status {
        height: 1;
        width: 1fr;
    }
    #gdb-pane {
        width: 1fr;
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #splitter {
        display: block;
    }
    #context-menu {
        display: none;
    }
    #context-menu.visible {
        display: block;
    }
    #file-dlg {
        layer: dialog;
        width: 1fr;
        height: 1fr;
        display: none;
        background: $surface;
    }
    #file-dlg.visible {
        display: block;
    }
    """

    def __init__(self, gdb_path: str = "gdb",
                 gdb_args: list[str] | None = None,
                 rc_file: Optional[str] = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = HighlightGroups()
        self.km = KeyMapper()
        self.cfg = Config()
        self.cp = ConfigParser(self.cfg, self.hl, self.km)
        self._initial_source_pending = bool(gdb_args)
        self._register_commands()
        if rc_file:
            self.cp.load_file(rc_file)
        else:
            self.cp.load_default_rc()

        self.gdb = GDBController(gdb_path=gdb_path, args=gdb_args or [])
        self._gdb_task: Optional[asyncio.Task] = None

        self._mode: str = "GDB"
        self._await_mark_jump: bool = False
        self._await_mark_set: bool = False
        self._split_ratio: float = 0.5
        self._cur_win_split: int = {
            "gdb_full": -2,
            "gdb_big": -1,
            "even": 0,
            "src_big": 1,
            "src_full": 2,
        }.get(self.cfg.winsplit.lower(), 0)
        self._window_shift: int = 0
        self._last_split_setting: str = ""
        self._last_orientation: str = ""
        self._preserve_window_shift_once: bool = False
        self._file_dialog_pending: bool = False
        self._inf_tty_fd: Optional[int] = None
        self._workspace_dynamic: bool = False
        self._context_menu_target: Optional[Widget] = None
        self._source_view: Optional[SourceView] = None
        self._gdb_widget: Optional[GDBWidget] = None
        self._locals_pane: Optional[LocalVariablePane] = None
        self._current_locals: list[LocalVariable] = []
        self._stack_pane: Optional[StackPane] = None
        self._current_stack: list[Frame] = []
        self._thread_pane: Optional[ThreadPane] = None
        self._current_threads: list[ThreadInfo] = []
        self._register_pane: Optional[RegisterPane] = None
        self._current_registers: list[RegisterInfo] = []
        self._pane_descriptors: dict[str, PaneDescriptor] = {
            "source": PaneDescriptor("Source", self._make_source_pane, lambda: self._source_view),
            "gdb": PaneDescriptor("GDB", self._make_gdb_pane, lambda: self._gdb_widget),
            "locals": PaneDescriptor(
                "Local Variables",
                self._make_local_variable_pane,
                lambda: self._locals_pane,
                lambda: self.gdb.request_current_frame_locals(report_error=False),
            ),
            "registers": PaneDescriptor(
                "Registers",
                self._make_register_pane,
                lambda: self._register_pane,
                lambda: self.gdb.request_current_registers(report_error=False),
            ),
            "stack": PaneDescriptor(
                "Stack",
                self._make_stack_pane,
                lambda: self._stack_pane,
                lambda: self.gdb.request_current_stack_frames(report_error=False),
            ),
            "threads": PaneDescriptor(
                "Threads",
                self._make_thread_pane,
                lambda: self._thread_pane,
                lambda: self.gdb.request_current_threads(report_error=False),
            ),
        }
        self._add_menu_order: tuple[str, ...] = (
            "source",
            "gdb",
            "locals",
            "registers",
            "threads",
            "stack",
        )

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        if self._source_view is None:
            self._source_view = SourceView(self.hl, id="src-pane")
        if self._gdb_widget is None:
            self._gdb_widget = GDBWidget(
                self.hl,
                max_scrollback=self.cfg.scrollbackbuffersize,
                id="gdb-pane",
            )
        with Widget(id="global-container"):
            with Widget(id="split-container"):
                yield self._source_view
                yield Splitter(self.hl, id="splitter")
                yield self._gdb_widget
            yield StatusBar(self.hl, id="status")
        yield FileDialog(self.hl, id="file-dlg")
        yield ContextMenu(self.hl, id="context-menu")

    # ------------------------------------------------------------------
    # on_mount — async so asyncio.create_task works
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        # Configure source widget
        src = self._get_source_view()
        gdb_w = self._get_gdb_widget()
        if src is None or gdb_w is None:
            self._show_status("Failed to initialize core panes")
            return
        src.executing_line_display = self.cfg.executinglinedisplay
        src.selected_line_display = self.cfg.selectedlinedisplay
        src.tabstop = self.cfg.tabstop
        src.hlsearch = self.cfg.hlsearch
        src.ignorecase = self.cfg.ignorecase
        src.wrapscan = self.cfg.wrapscan
        src.showmarks = self.cfg.showmarks

        # Configure GDB widget
        gdb_w.ignorecase = self.cfg.ignorecase
        gdb_w.wrapscan = self.cfg.wrapscan
        gdb_w.send_to_gdb = self.gdb.send_input        # bytes → primary PTY
        gdb_w.resize_gdb = self.gdb.resize             # keep pyte in sync
        gdb_w.on_switch_to_cgdb = self._switch_to_cgdb

        # Configure file dialog
        fd = self.query_one("#file-dlg", FileDialog)
        fd.ignorecase = self.cfg.ignorecase
        fd.wrapscan = self.cfg.wrapscan

        # GDB callbacks — on_console now delivers raw bytes from GDB's PTY,
        # fed directly into the pyte VT100 emulator (matching cgdb's libvterm).
        self.gdb.on_console = lambda data: self.call_later(gdb_w.feed_bytes, data)
        self.gdb.on_stopped = lambda f: self.call_later(self._ui_on_stopped, f)
        self.gdb.on_running = lambda: self.call_later(self._ui_on_running)
        self.gdb.on_breakpoints = lambda b: self.call_later(self._ui_set_breakpoints, b)
        self.gdb.on_source_files = lambda f: self.call_later(self._ui_set_source_files, f)
        self.gdb.on_source_file = lambda f, l: self.call_later(self._ui_load_source_file, f, l)
        self.gdb.on_locals = lambda v: self.call_later(self._ui_set_locals, v)
        self.gdb.on_registers = lambda v: self.call_later(self._ui_set_registers, v)
        self.gdb.on_stack = lambda v: self.call_later(self._ui_set_stack, v)
        self.gdb.on_threads = lambda v: self.call_later(self._ui_set_threads, v)
        self.gdb.on_exit = lambda: self.call_later(self._ui_gdb_exit)
        self.gdb.on_error = lambda m: self.call_later(self._show_status, f"Error: {m}")

        # Start GDB process
        try:
            self.gdb.start(rows=40, cols=200)
        except Exception as e:
            self._show_status(f"Failed to start GDB: {e}")
            return

        # Start async read loop
        self._gdb_task = asyncio.create_task(self.gdb.run_async())
        asyncio.ensure_future(self._request_initial_location())

        # Initial mode: GDB focused
        self._set_mode("GDB")
        gdb_w.focus()

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        try:
            status = self.query_one("#status", StatusBar)
            status.set_mode(mode)
            self._update_status_file_info()
        except NoMatches:
            pass
        # cgdb: scr_refresh(gdb_scroller, focus==GDB, ...) — hide GDB cursor when not focused
        gdb_w = self._get_gdb_widget()
        if gdb_w is not None:
            gdb_w.gdb_focused = (mode in ("GDB", "SCROLL"))
            gdb_w.refresh()

    def _switch_to_cgdb(self) -> None:
        self._set_mode("CGDB")
        if self._focus_widget(self._get_source_view(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())

    def _switch_to_gdb(self) -> None:
        self._set_mode("GDB")
        if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())

    def _show_status(self, msg: str) -> None:
        try:
            self.query_one("#status", StatusBar).show_message(msg)
        except NoMatches:
            pass

    @staticmethod
    def _widget_attached(widget: Optional[Widget]) -> bool:
        return widget is not None and widget.parent is not None

    def _get_source_view(self, *, mounted_only: bool = False) -> Optional[SourceView]:
        if self._source_view is None:
            return None
        if mounted_only and not self._widget_attached(self._source_view):
            return None
        return self._source_view

    def _get_gdb_widget(self, *, mounted_only: bool = False) -> Optional[GDBWidget]:
        if self._gdb_widget is None:
            return None
        if mounted_only and not self._widget_attached(self._gdb_widget):
            return None
        return self._gdb_widget

    def _focus_widget(self, widget: Optional[Widget]) -> bool:
        if not self._widget_attached(widget):
            return False
        try:
            widget.focus()
        except Exception:
            return False
        return True

    def _first_workspace_leaf(self, widget: Optional[Widget] = None) -> Optional[Widget]:
        if widget is None:
            if not self._workspace_dynamic:
                return (
                    self._get_source_view(mounted_only=True)
                    or self._get_gdb_widget(mounted_only=True)
                )
            try:
                widget = self.query_one("#split-container", PaneContainer)
            except NoMatches:
                return None

        if isinstance(widget, PaneContainer):
            for item in widget.items:
                leaf = self._first_workspace_leaf(item)
                if leaf is not None:
                    return leaf
            return None

        if getattr(widget, "can_focus", False) and self._widget_attached(widget):
            return widget
        return None

    def _make_source_pane(self) -> SourceView:
        if self._source_view is None:
            self._source_view = SourceView(self.hl, id="src-pane")
        return self._source_view

    def _make_gdb_pane(self) -> GDBWidget:
        if self._gdb_widget is None:
            self._gdb_widget = GDBWidget(
                self.hl,
                max_scrollback=self.cfg.scrollbackbuffersize,
                id="gdb-pane",
            )
        return self._gdb_widget

    def _make_local_variable_pane(self) -> LocalVariablePane:
        if self._locals_pane is None:
            self._locals_pane = LocalVariablePane(self.hl)
        self._locals_pane.set_variables(self._current_locals)
        return self._locals_pane

    def _make_register_pane(self) -> RegisterPane:
        if self._register_pane is None:
            self._register_pane = RegisterPane(self.hl)
        self._register_pane.set_registers(self._current_registers)
        return self._register_pane

    def _make_stack_pane(self) -> StackPane:
        current_level = self.gdb.current_frame.level if self.gdb.current_frame else 0
        if self._stack_pane is None:
            self._stack_pane = StackPane(self.hl)
        self._stack_pane.set_frames(self._current_stack, current_level=current_level)
        return self._stack_pane

    def _make_thread_pane(self) -> ThreadPane:
        if self._thread_pane is None:
            self._thread_pane = ThreadPane(self.hl)
        self._thread_pane.set_threads(self._current_threads)
        return self._thread_pane

    def _pane_widget(self, pane_kind: str) -> Optional[Widget]:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is None:
            return None
        return descriptor.current()

    def _pane_label(self, pane_kind: str) -> str:
        descriptor = self._pane_descriptors.get(pane_kind)
        return descriptor.label if descriptor is not None else pane_kind

    def _pane_is_attached(self, pane_kind: str) -> bool:
        return self._widget_attached(self._pane_widget(pane_kind))

    def _pane_kind_for_widget(self, widget: Widget) -> Optional[str]:
        for pane_kind, descriptor in self._pane_descriptors.items():
            if widget is descriptor.current():
                return pane_kind
        return None

    def _create_pane(self, pane_kind: str) -> Optional[Widget]:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is None:
            return None
        return descriptor.create()

    def _get_context_menu(self) -> Optional[ContextMenu]:
        try:
            return self.query_one("#context-menu", ContextMenu)
        except NoMatches:
            return None

    def _context_menu_contains(self, screen_x: int, screen_y: int) -> bool:
        menu = self._get_context_menu()
        if not menu or not menu.is_open:
            return False
        return menu.contains_point(screen_x, screen_y)

    def _build_context_menu_items(self) -> list[ContextMenuItem]:
        add_children = [
            ContextMenuItem(
                self._pane_label(pane_kind),
                action=f"add:{pane_kind}",
            )
            for pane_kind in self._add_menu_order
            if not self._pane_is_attached(pane_kind)
        ]
        if not add_children:
            add_children = [ContextMenuItem("No panes available")]

        split_children = [
            ContextMenuItem("⬒ Up", action="split:up"),
            ContextMenuItem("⬓ Down", action="split:down"),
            ContextMenuItem("◧ Left", action="split:left"),
            ContextMenuItem("◨ Right", action="split:right"),
        ]
        return [
            ContextMenuItem("Add", children=tuple(add_children)),
            ContextMenuItem("Split", children=tuple(split_children)),
            ContextMenuItem("Hide", action="hide", separator_before=True),
            ContextMenuItem("Delete", action="delete"),
        ]

    def _open_context_menu(self, screen_x: int, screen_y: int) -> None:
        menu = self._get_context_menu()
        if not menu:
            return
        menu.set_items(self._build_context_menu_items())
        menu.open_at(screen_x, screen_y)

    def _restore_focus_after_context_menu(self) -> None:
        if self._mode == "FILEDLG":
            try:
                self.query_one("#file-dlg", FileDialog).focus()
                return
            except NoMatches:
                return
        if self._mode == "STATUS":
            try:
                self.query_one("#status", StatusBar).focus()
                return
            except NoMatches:
                return
        if self._mode in ("GDB", "SCROLL"):
            if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
                return
        if self._focus_widget(self._get_source_view(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())

    def _close_context_menu(self, *, restore_focus: bool = True) -> None:
        menu = self._get_context_menu()
        if not menu or not menu.is_open:
            return
        menu.close()
        self._context_menu_target = None
        if restore_focus:
            self._restore_focus_after_context_menu()

    def _find_workspace_item(self, widget: Optional[Widget]) -> Optional[Widget]:
        current = widget
        while isinstance(current, Widget):
            if isinstance(current, Splitter):
                return None
            parent = current.parent
            if isinstance(parent, PaneContainer):
                return current
            if getattr(parent, "id", None) == "split-container" and getattr(current, "id", None) in {
                "src-pane",
                "gdb-pane",
            }:
                return current
            current = parent if isinstance(parent, Widget) else None
        return None

    async def _ensure_dynamic_workspace(self) -> Optional[PaneContainer]:
        if self._workspace_dynamic:
            try:
                return self.query_one("#split-container", PaneContainer)
            except NoMatches:
                return None

        # Set the flag immediately to prevent a second concurrent call from
        # entering the same transformation path before the first one finishes.
        self._workspace_dynamic = True

        try:
            old_root = self.query_one("#split-container")
            global_container = self.query_one("#global-container")
            status = self.query_one("#status")
            splitter = self.query_one("#splitter", Splitter)
        except NoMatches:
            return None
        src = self._get_source_view(mounted_only=True)
        gdb = self._get_gdb_widget(mounted_only=True)
        if src is None or gdb is None:
            return None

        new_root = PaneContainer(
            self.hl,
            orientation=self.cfg.winsplitorientation,
            id="split-container",
        )
        async with global_container.batch():
            await old_root.remove_children([src, splitter, gdb])
            await old_root.remove()
            await global_container.mount(new_root, before=status)
        await new_root.set_items([src, gdb])
        return new_root

    async def _replace_workspace_item(self, target: Widget, new_item: Widget) -> bool:
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
        if parent is None:
            return False
        await parent.replace_item(target, new_item)
        return True

    async def _normalize_container_after_delete(self, container: PaneContainer) -> Optional[Widget]:
        current = container
        while True:
            parent = current.parent if isinstance(current.parent, PaneContainer) else None
            item_count = len(current.items)
            if parent is None:
                if item_count == 0:
                    await current.set_items([EmptyPane()])
                return self._first_workspace_leaf()

            if item_count > 1:
                return self._first_workspace_leaf()

            if item_count == 1:
                remaining = await current.take_item(current.items[0])
                await parent.replace_item(current, remaining)
            else:
                await parent.replace_item(current, EmptyPane())
            current = parent

    async def _add_pane_to_workspace(self, target: Widget, pane_kind: str) -> Optional[Widget]:
        pane = self._create_pane(pane_kind)
        if pane is None:
            return None
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
        if parent is None:
            return None

        if isinstance(target, EmptyPane):
            await parent.replace_item(target, pane)
        else:
            index = parent.index_of(target) + 1
            await parent.insert_item(index, pane)

        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is not None and descriptor.requester is not None:
            descriptor.requester()
        return pane

    async def _hide_workspace_item(self, target: Widget) -> Optional[Widget]:
        if isinstance(target, EmptyPane):
            return target
        replacement = EmptyPane()
        if await self._replace_workspace_item(target, replacement):
            return replacement
        return None

    async def _delete_workspace_item(self, target: Widget) -> Optional[Widget]:
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
        if parent is None:
            return None
        await parent.take_item(target)
        return await self._normalize_container_after_delete(parent)

    async def _apply_context_menu_action(self, target: Widget, direction: str) -> bool:
        axis = "horizontal" if direction in ("left", "right") else "vertical"
        insert_before = direction in ("left", "up")
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
        if parent is None:
            return False

        if parent.orientation == axis:
            index = parent.index_of(target)
            if not insert_before:
                index += 1
            await parent.insert_item(index, EmptyPane())
            return True

        new_container = PaneContainer(self.hl, orientation=axis)
        await parent.replace_item(target, new_container)
        if insert_before:
            await new_container.set_items([EmptyPane(), target])
        else:
            await new_container.set_items([target, EmptyPane()])
        return True

    def _handle_pending_mark_key(self, char: str) -> bool:
        src = self._get_source_view(mounted_only=True)
        if src is None:
            self._await_mark_jump = False
            self._await_mark_set = False
            return False

        if self._await_mark_jump:
            self._await_mark_jump = False
            if char == ".":
                src.goto_executing()
            elif char == "'":
                src.goto_last_jump()
            elif char.isalpha():
                src.jump_to_mark(char)
            return True

        if self._await_mark_set:
            self._await_mark_set = False
            if char.isalpha():
                src.set_mark(char)
            return True

        return False

    def _handle_cgdb_mode_key(self, key: str, char: str) -> bool:
        if self._mode != "CGDB":
            return False

        src = self._get_source_view(mounted_only=True)

        if src is not None and src.handle_cgdb_key(key, char):
            return True

        if key == "i":
            self._switch_to_gdb()
            return True
        if key == "s":
            self._switch_to_gdb()
            gdb_w = self._get_gdb_widget(mounted_only=True)
            if gdb_w is not None:
                gdb_w.enter_scroll_mode()
            return True

        return False

    def _handle_non_gdb_focus_key(self, key: str, char: str) -> bool:
        """Absorb keys that arrive at GDB during focus handoff to CGDB/STATUS."""
        if self._handle_pending_mark_key(char):
            return True

        if self._mode == "STATUS":
            try:
                self.query_one("#status", StatusBar).feed_key(key, char)
            except NoMatches:
                pass
            return True

        if self._mode == "CGDB":
            self._handle_cgdb_mode_key(key, char)
            return True

        return False

    # ------------------------------------------------------------------
    # Global key handling
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""
        menu = self._get_context_menu()

        if menu and menu.is_open:
            if key == "escape":
                self._close_context_menu()
            event.stop()
            return

        if self._handle_pending_mark_key(char):
            event.stop()
            return

        # ESC / cgdb mode key → switch to CGDB from GDB/STATUS/SCROLL
        cgdb_key = self.cfg.cgdbmodekey.lower()
        if key == "escape" or key.lower() == cgdb_key:
            if self._mode in ("GDB", "STATUS", "SCROLL"):
                self._switch_to_cgdb()
                event.stop()
                return

        if self._mode == "STATUS":
            status = self.query_one("#status", StatusBar)
            if status.feed_key(key, char):
                event.stop()
                return

        if self._handle_cgdb_mode_key(key, char):
            event.stop()
            return

        # Ctrl-C always interrupts GDB
        if key == "ctrl+c":
            self.gdb.send_interrupt()
            event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        menu = self._get_context_menu()
        screen_x = int(event.screen_x)
        screen_y = int(event.screen_y)

        if event.button == 3:
            try:
                clicked_widget, _ = self.get_widget_at(screen_x, screen_y)
            except Exception:
                clicked_widget = event.widget
            target = self._find_workspace_item(clicked_widget)
            if target is not None:
                self._context_menu_target = target
                self._open_context_menu(screen_x, screen_y)
                event.stop()
                return

        if menu and menu.is_open and event.button == 1:
            if not self._context_menu_contains(screen_x, screen_y):
                self._close_context_menu()
                event.stop()

    # ------------------------------------------------------------------
    # Source widget messages
    # ------------------------------------------------------------------

    def on_toggle_breakpoint(self, msg: ToggleBreakpoint) -> None:
        src = self._get_source_view()
        if src is None:
            self._show_status("No source pane available")
            return
        sf = src.source_file
        if not sf:
            self._show_status("No source file loaded")
            return
        existing = next(
            (b for b in self.gdb.breakpoints
             if b.line == msg.line and
             os.path.basename(b.fullname or b.file) == os.path.basename(sf.path)),
            None
        )
        if existing:
            self.gdb.delete_breakpoint(existing.number)
        else:
            self.gdb.set_breakpoint(f"{sf.path}:{msg.line}",
                                    temporary=msg.temporary)

    def on_open_file_dialog(self, msg: OpenFileDialog) -> None:
        # Open immediately and show a pending state while GDB enumerates files.
        # Large binaries can make -file-list-exec-source-files noticeably slow.
        try:
            fd = self.query_one("#file-dlg", FileDialog)
        except NoMatches:
            self._show_status("File dialog is unavailable")
            return
        self._file_dialog_pending = True
        fd.open_pending()
        self._set_mode("FILEDLG")
        self.gdb.request_source_files()

    def on_open_tty(self, _: OpenTTY) -> None:
        """Ctrl-T: allocate a new PTY for the inferior's stdio.
        Mirrors cgdb: open a PTY pair and tell GDB 'set inferior-tty <slave>'."""
        try:
            master_fd, slave_fd = os.openpty()
            slave_path = os.ttyname(slave_fd)
            os.close(slave_fd)
            # Store master_fd so it stays open (inferior can write to slave)
            if hasattr(self, '_inf_tty_fd') and self._inf_tty_fd is not None:
                try:
                    os.close(self._inf_tty_fd)
                except OSError:
                    pass
            self._inf_tty_fd = master_fd
            self.gdb.send_input(f"set inferior-tty {slave_path}\n")
            self._show_status(f"Inferior TTY: {slave_path}")
        except OSError as e:
            self._show_status(f"TTY error: {e}")

    def on_await_mark_jump(self, msg: AwaitMarkJump) -> None:
        self._await_mark_jump = True

    def on_await_mark_set(self, msg: AwaitMarkSet) -> None:
        self._await_mark_set = True

    def on_jump_global_mark(self, msg: JumpGlobalMark) -> None:
        src = self._get_source_view()
        if src is not None and src.load_file(msg.path):
            src.move_to(msg.line)

    def on_search_start(self, msg: SearchStart) -> None:
        self.query_one("#status", StatusBar).start_search(msg.forward)

    def on_search_update(self, msg: SearchUpdate) -> None:
        self.query_one("#status", StatusBar).update_search(msg.pattern)

    def on_search_commit(self, msg: SearchCommit) -> None:
        self.query_one("#status", StatusBar).cancel_input()
        self._set_mode("CGDB")

    def on_search_cancel(self, msg: SearchCancel) -> None:
        self.query_one("#status", StatusBar).cancel_input()
        self._set_mode("CGDB")

    def on_status_message(self, msg: StatusMessage) -> None:
        self._show_status(msg.text)

    _WIN_SPLIT_FREE = -3
    _SPLIT_MARKS = {
        "gdb_full": -2,
        "gdb_big": -1,
        "even": 0,
        "src_big": 1,
        "src_full": 2,
    }
    _SPLIT_NAMES = {value: key for key, value in _SPLIT_MARKS.items()}

    def _split_axis(self, is_horizontal: bool) -> int:
        try:
            container = self.query_one("#split-container")
            axis = container.size.width if is_horizontal else container.size.height
            if axis:
                return max(1, axis)
        except NoMatches:
            pass
        return max(1, self.size.width if is_horizontal else max(1, self.size.height - 1))

    def _pane_axis(self, is_horizontal: bool) -> int:
        return max(0, self._split_axis(is_horizontal) - 1)

    def _reset_window_shift(self, is_horizontal: bool) -> None:
        half_axis = self._pane_axis(is_horizontal) // 2
        self._window_shift = int(half_axis * (self._cur_win_split / 2.0))
        self._validate_window_shift(is_horizontal)

    def _set_window_shift_from_ratio(self, is_horizontal: bool, ratio: float) -> None:
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            self._window_shift = 0
            return
        target_src = int(round(axis * ratio))
        self._window_shift = target_src - (axis // 2)
        self._validate_window_shift(is_horizontal)

    def _validate_window_shift(self, is_horizontal: bool) -> None:
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            self._window_shift = 0
            return
        base = axis // 2
        min_size = self.cfg.winminwidth if is_horizontal else self.cfg.winminheight
        min_shift = min_size - base
        max_shift = (axis - min_size) - base

        if max_shift < min_shift:
            max_shift = min_shift = 0

        if self._window_shift > max_shift:
            self._window_shift = max_shift
        elif self._window_shift < min_shift:
            self._window_shift = min_shift

    def _compute_split_sizes(self, is_horizontal: bool, axis: int | None = None) -> tuple[int, int]:
        axis = self._pane_axis(is_horizontal) if axis is None else max(0, axis)
        if self._cur_win_split == -2:
            return 0, axis
        if self._cur_win_split == 2:
            return axis, 0
        src_size = (axis // 2) + self._window_shift
        src_size = max(0, min(axis, src_size))
        gdb_size = max(0, axis - src_size)
        return src_size, gdb_size

    def on_resize_source(self, msg: ResizeSource) -> None:
        if self._workspace_dynamic:
            return
        is_horizontal = (self.cfg.winsplitorientation == "horizontal")
        half_axis = self._split_axis(is_horizontal) // 2

        if msg.rows:
            # cgdb '=' / '-': change window_shift by exactly 1 unit
            self._cur_win_split = self._WIN_SPLIT_FREE
            self._window_shift += msg.delta
            self._validate_window_shift(is_horizontal)
            self.cfg.winsplit = "free"
            self._apply_split()

        elif msg.jump:
            # cgdb '+' / '_': jump to the next quarter-mark split.
            split = self._cur_win_split
            if split == self._WIN_SPLIT_FREE and half_axis > 0:
                split = int((2 * self._window_shift) / half_axis)

            if msg.delta > 0:
                if self._cur_win_split == self._WIN_SPLIT_FREE and self._window_shift > 0:
                    split += 1
                elif self._cur_win_split != self._WIN_SPLIT_FREE:
                    split += 1
                split = min(2, split)
            else:
                if self._cur_win_split == self._WIN_SPLIT_FREE and self._window_shift < 0:
                    split -= 1
                elif self._cur_win_split != self._WIN_SPLIT_FREE:
                    split -= 1
                split = max(-2, split)

            self._cur_win_split = split
            self._window_shift = int(half_axis * (split / 2.0))
            self._validate_window_shift(is_horizontal)
            self.cfg.winsplit = self._SPLIT_NAMES[split]
            self._apply_split()

        else:
            # legacy percent mode
            axis = self._split_axis(is_horizontal)
            self._cur_win_split = self._WIN_SPLIT_FREE
            self._window_shift += int((axis * msg.delta) / 100)
            self._validate_window_shift(is_horizontal)
            self.cfg.winsplit = "free"
            self._apply_split()

    def on_toggle_orientation(self, _: ToggleOrientation) -> None:
        new_orientation = (
            "vertical" if self.cfg.winsplitorientation == "horizontal"
            else "horizontal"
        )
        self.cfg.winsplitorientation = new_orientation
        if self._workspace_dynamic:
            try:
                self.query_one("#split-container", PaneContainer).set_orientation(new_orientation)
            except NoMatches:
                pass
            return
        self._set_window_shift_from_ratio(new_orientation == "horizontal", self._split_ratio)
        self._preserve_window_shift_once = True
        self._apply_split()

    def on_gdb_command(self, msg: GDBCommand) -> None:
        self._send_gdb_cli(msg.cmd)

    def on_show_help(self, _: ShowHelp) -> None:
        self._show_help_in_source()

    # ------------------------------------------------------------------
    # GDB widget messages
    # ------------------------------------------------------------------

    def on_scroll_mode_change(self, msg: ScrollModeChange) -> None:
        self._set_mode("SCROLL" if msg.active else "GDB")

    def on_scroll_search_start(self, msg: ScrollSearchStart) -> None:
        self.query_one("#status", StatusBar).start_search(msg.forward)

    def on_scroll_search_update(self, msg: ScrollSearchUpdate) -> None:
        self.query_one("#status", StatusBar).update_search(msg.pattern)

    def on_scroll_search_commit(self, msg: ScrollSearchCommit) -> None:
        self.query_one("#status", StatusBar).cancel_input()

    def on_scroll_search_cancel(self, msg: ScrollSearchCancel) -> None:
        self.query_one("#status", StatusBar).cancel_input()

    # ------------------------------------------------------------------
    # Status bar command handling
    # ------------------------------------------------------------------

    def on_command_submit(self, msg: CommandSubmit) -> None:
        err = self.cp.execute(msg.command)
        if err:
            self._show_status(err)
        else:
            self._sync_config()
        self._switch_to_cgdb()

    def on_command_cancel(self, msg: CommandCancel) -> None:
        self._switch_to_cgdb()

    # ------------------------------------------------------------------
    # File dialog
    # ------------------------------------------------------------------

    def on_file_selected(self, msg: FileSelected) -> None:
        self._file_dialog_pending = False
        self.query_one("#file-dlg", FileDialog).close()
        src = self._get_source_view()
        if src is not None:
            src.load_file(msg.path)
            self._update_status_file_info()
        self._switch_to_cgdb()

    def on_file_dialog_closed(self, _: FileDialogClosed) -> None:
        self._file_dialog_pending = False
        self.query_one("#file-dlg", FileDialog).close()
        self._switch_to_cgdb()

    async def on_context_menu_selected(self, msg: ContextMenuSelected) -> None:
        target = self._context_menu_target
        self._close_context_menu(restore_focus=False)
        if target is None:
            return
        if await self._ensure_dynamic_workspace() is None:
            self._show_status("Unable to create workspace container")
            self._restore_focus_after_context_menu()
            return

        action = msg.action
        focus_target: Optional[Widget] = None

        if action.startswith("add:"):
            pane_kind = action.split(":", 1)[1]
            if self._pane_is_attached(pane_kind):
                self._show_status(f"{self._pane_label(pane_kind)} is already shown")
                self._restore_focus_after_context_menu()
                return
            focus_target = await self._add_pane_to_workspace(target, pane_kind)
            if focus_target is None:
                self._show_status(f"Unable to add {self._pane_label(pane_kind)}")
                self._restore_focus_after_context_menu()
                return
            self._show_status(f"Added {self._pane_label(pane_kind)}")
            self._focus_widget(focus_target)
            return

        if action == "hide":
            pane_kind = self._pane_kind_for_widget(target)
            if isinstance(target, EmptyPane):
                self._show_status("Cell is already empty")
                self._focus_widget(target)
                return
            focus_target = await self._hide_workspace_item(target)
            if focus_target is None:
                self._show_status("Unable to hide cell")
                self._restore_focus_after_context_menu()
                return
            label = self._pane_label(pane_kind) if pane_kind is not None else "pane"
            self._show_status(f"Hid {label}")
            self._focus_widget(focus_target)
            return

        if action == "delete":
            focus_target = await self._delete_workspace_item(target)
            if focus_target is None:
                self._show_status("Unable to delete cell")
                self._restore_focus_after_context_menu()
                return
            self._show_status("Deleted cell")
            self._focus_widget(focus_target)
            return

        direction = action.split(":", 1)[1] if action.startswith("split:") else None
        if direction is None:
            self._show_status(f"Context menu: {action}")
            self._restore_focus_after_context_menu()
            return

        if await self._apply_context_menu_action(target, direction):
            self._show_status(f"Added window {direction}")
            self._focus_widget(target)
        else:
            self._show_status(f"Unable to add window {direction}")
            self._restore_focus_after_context_menu()

    def on_context_menu_closed(self, _: ContextMenuClosed) -> None:
        self._close_context_menu()

    # ------------------------------------------------------------------
    # GDB UI callbacks (scheduled via call_later — runs on main event loop)
    # ------------------------------------------------------------------

    def _ui_on_stopped(self, frame: Frame) -> None:
        """GDB stopped — update source view to executing location."""
        path = frame.fullname or frame.file
        if path and os.path.isfile(path):
            src = self._get_source_view()
            if src is not None:
                if not src.source_file or src.source_file.path != path:
                    src.load_file(path)
                elif self.cfg.autosourcereload:
                    src.reload_if_changed()
                src.exe_line = frame.line
                src.move_to(frame.line)
                self._update_status_file_info()
        # Refresh the full source-file list only when the file dialog is waiting
        # for it; on large binaries this MI query can block interactive GDB input
        # long enough to make the console look hung after commands like `start`.
        if self._file_dialog_pending:
            self.gdb.request_source_files()
        asyncio.ensure_future(self._refresh_breakpoints_async())

    async def _refresh_breakpoints_async(self) -> None:
        await asyncio.sleep(0.15)
        self.gdb.mi_command("-break-list")

    def _ui_on_running(self) -> None:
        self._set_mode("GDB")
        if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())

    def _ui_set_breakpoints(self, bps: list[Breakpoint]) -> None:
        src = self._get_source_view()
        if src is not None:
            src.set_breakpoints(bps)

    def _ui_set_locals(self, variables: list[LocalVariable]) -> None:
        self._current_locals = list(variables)
        if self._locals_pane is not None:
            self._locals_pane.set_variables(self._current_locals)

    def _ui_set_registers(self, registers: list[RegisterInfo]) -> None:
        self._current_registers = list(registers)
        if self._register_pane is not None:
            self._register_pane.set_registers(self._current_registers)

    def _ui_set_stack(self, frames: list[Frame]) -> None:
        self._current_stack = list(frames)
        current_level = self.gdb.current_frame.level if self.gdb.current_frame else 0
        if self._stack_pane is not None:
            self._stack_pane.set_frames(self._current_stack, current_level=current_level)

    def _ui_set_threads(self, threads: list[ThreadInfo]) -> None:
        self._current_threads = list(threads)
        if self._thread_pane is not None:
            self._thread_pane.set_threads(self._current_threads)

    def _ui_set_source_files(self, files: list[str]) -> None:
        try:
            fd = self.query_one("#file-dlg", FileDialog)
        except NoMatches:
            self._file_dialog_pending = False
            return
        pending = self._file_dialog_pending
        self._file_dialog_pending = False
        if not pending or not fd.is_open:
            return
        fd.files = files

    def _ui_load_source_file(self, path: str, line: int = 0) -> None:
        """Load a specific source file (from -file-list-exec-source-file)."""
        if not os.path.isfile(path):
            return
        src = self._get_source_view()
        if src is None:
            return
        # Only load if no file is shown yet (don't override a user selection)
        if not src.source_file:
            src.load_file(path)
            if line > 0:
                src.move_to(line)
            self._initial_source_pending = False
            src.run_pending_search()
            self._update_status_file_info()

    async def _request_initial_location(self) -> None:
        """Mirror cgdb startup: query current location without surfacing noise."""
        await asyncio.sleep(0.5)
        self.gdb.request_current_location(report_error=False)

    def _ui_gdb_exit(self) -> None:
        # Mirror cgdb: when GDB exits (EOF/error on primary PTY), exit immediately.
        # cgdb calls cgdb_cleanup_and_exit(0) in tgdb_process() on size<=0.
        self.exit(0)

    # ------------------------------------------------------------------
    # Registered commands
    # ------------------------------------------------------------------

    def _register_commands(self) -> None:
        def gdb_cmd(c):
            return lambda a: self._send_gdb_cli(c) or None

        cmds = {
            "bang": self._cmd_bang,
            "quit": self._cmd_quit, "q": self._cmd_quit,
            "help": self._cmd_help,
            "edit": self._cmd_edit, "e": self._cmd_edit,
            "focus": self._cmd_focus,
            "insert": lambda a: self._switch_to_gdb() or None,
            "noh": self._cmd_noh,
            "shell": self._cmd_shell, "sh": self._cmd_shell,
            "logo": self._cmd_logo,
            "syntax": self._cmd_syntax,
            "capturescreen": self._cmd_capturescreen, "cs": self._cmd_capturescreen,
            "continue": gdb_cmd("continue"), "c": gdb_cmd("continue"),
            "next": gdb_cmd("next"), "n": gdb_cmd("next"),
            "nexti": gdb_cmd("nexti"),
            "step": gdb_cmd("step"), "s": gdb_cmd("step"),
            "stepi": gdb_cmd("stepi"),
            "finish": gdb_cmd("finish"), "f": gdb_cmd("finish"),
            "run": gdb_cmd("run"), "r": gdb_cmd("run"),
            "start": gdb_cmd("start"),
            "kill": gdb_cmd("kill"), "k": gdb_cmd("kill"),
            "until": gdb_cmd("until"), "u": gdb_cmd("until"),
            "up": gdb_cmd("up"),
            "down": gdb_cmd("down"),
        }
        for name, fn in cmds.items():
            self.cp.register_handler(name, fn)

    def _cmd_quit(self, _: list) -> None:
        self.gdb.terminate()
        self.exit(0)

    def _cmd_help(self, _: list) -> None:
        self._show_help_in_source()

    def _cmd_logo(self, _: list) -> None:
        src = self._get_source_view()
        if src is not None:
            src.show_logo()

    def _cmd_edit(self, _: list) -> None:
        src = self._get_source_view()
        if src is not None and src.source_file:
            src.source_file._tokens = None
            src.load_file(src.source_file.path)

    def _cmd_bang(self, _: list) -> None:
        # cgdb registers :bang, but command_do_bang() is currently a no-op.
        return None

    def _cmd_focus(self, args: list) -> Optional[str]:
        if len(args) != 1:
            return "focus: requires cgdb or gdb"
        if args[0].lower() == "gdb":
            self._switch_to_gdb()
            return None
        if args[0].lower() == "cgdb":
            self._switch_to_cgdb()
            return None
        return "focus: requires cgdb or gdb"

    def _cmd_noh(self, _: list) -> None:
        self.cfg.hlsearch = False
        src = self._get_source_view()
        if src is not None:
            src.hlsearch = False
            src.refresh()

    def _cmd_syntax(self, args: list) -> None:
        """Mirror cgdb's :syntax [on|off|c|asm|…] command."""
        value = args[0] if args else ""
        if value:
            self.cp._set_option("syntax", value)
        # No args: cgdb prints info (TODO); we just refresh
        self._sync_config()

    def _cmd_shell(self, args: list) -> Optional[str]:
        import os
        import shlex
        import subprocess

        try:
            with self.suspend():
                if args:
                    subprocess.call(shlex.join(args), shell=True)
                else:
                    subprocess.call([os.environ.get("SHELL", "/bin/sh")])
                try:
                    input("Hit ENTER to continue...")
                except EOFError:
                    pass
        except Exception as e:
            return str(e)
        self.refresh()
        return None

    def _cmd_capturescreen(self, args: list) -> Optional[str]:
        """Save an SVG screenshot of the current screen.

        :capturescreen            — saves to tgdb-<nanosecond-timestamp>.svg
        :capturescreen myfile.svg — saves to myfile.svg
        """
        try:
            if args:
                filename = args[0]
            else:
                ns = time.time_ns()
                dt = datetime.fromtimestamp(ns // 1_000_000_000)
                nano = ns % 1_000_000_000
                ts = dt.strftime('%Y-%m-%d-%H-%M-%S-') + f"{nano:09d}"
                filename = f"tgdb-{ts}.svg"
            path = self.save_screenshot(filename=filename)
            self._show_status(f"Screenshot saved: {path}")
        except Exception as e:
            return str(e)
        return None

    def _send_gdb_cli(self, cmd: str) -> None:
        if self.cfg.showdebugcommands:
            # Mirror cgdb showdebugcommands: echo the command into the GDB window
            gdb_w = self._get_gdb_widget()
            if gdb_w is not None:
                gdb_w.inject_text(f"(gdb) {cmd}\n")
        self.gdb.send_input(cmd + "\n")
        command_name = cmd.strip().split(None, 1)[0].lower() if cmd.strip() else ""
        if command_name in {"up", "down", "frame", "f", "select-frame", "thread"}:
            asyncio.get_running_loop().call_later(
                0.1,
                lambda: self.gdb.request_current_location(report_error=False),
            )
        self._switch_to_gdb()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _apply_split(self) -> None:
        if self._workspace_dynamic:
            try:
                self.query_one("#split-container", PaneContainer).set_orientation(
                    self.cfg.winsplitorientation
                )
            except NoMatches:
                pass
            self._last_orientation = self.cfg.winsplitorientation
            return
        split = self.cfg.winsplit.lower()
        is_horizontal = (self.cfg.winsplitorientation == "horizontal")
        split_changed = split != self._last_split_setting
        orientation_changed = (
            self.cfg.winsplitorientation != self._last_orientation
        )

        if split in self._SPLIT_MARKS:
            self._cur_win_split = self._SPLIT_MARKS[split]
            if split_changed:
                self._reset_window_shift(is_horizontal)
            elif orientation_changed and not self._preserve_window_shift_once:
                self._set_window_shift_from_ratio(is_horizontal, self._split_ratio)
        elif orientation_changed and not self._preserve_window_shift_once:
            self._set_window_shift_from_ratio(is_horizontal, self._split_ratio)

        self._validate_window_shift(is_horizontal)
        total_axis = max(1, self._split_axis(is_horizontal))
        src_size, gdb_size = self._compute_split_sizes(is_horizontal)
        show_splitter = (src_size > 0 and gdb_size > 0)
        if not show_splitter:
            src_size, gdb_size = self._compute_split_sizes(is_horizontal, total_axis)
        pane_total = max(1, src_size + gdb_size)
        self._split_ratio = src_size / pane_total

        try:
            container = self.query_one("#split-container")
            splitter = self.query_one("#splitter", Splitter)
            src = self._get_source_view(mounted_only=True)
            gdb = self._get_gdb_widget(mounted_only=True)
            if src is None or gdb is None:
                return
            splitter.set_orientation(is_horizontal)
            splitter.styles.display = "block" if show_splitter else "none"
            if is_horizontal:
                # Horizontal container: source on the left, GDB on the right.
                container.styles.layout = "horizontal"
                src.styles.display = "none" if src_size <= 0 else "block"
                gdb.styles.display = "none" if gdb_size <= 0 else "block"
                src.styles.width = src_size
                src.styles.height = "1fr"
                gdb.styles.width = gdb_size
                gdb.styles.height = "1fr"
            else:
                # Vertical container: source on top, GDB below.
                container.styles.layout = "vertical"
                src.styles.display = "none" if src_size <= 0 else "block"
                gdb.styles.display = "none" if gdb_size <= 0 else "block"
                src.styles.width = "1fr"
                src.styles.height = src_size
                gdb.styles.width = "1fr"
                gdb.styles.height = gdb_size
        except NoMatches:
            pass
        finally:
            self._last_split_setting = split
            self._last_orientation = self.cfg.winsplitorientation
            self._preserve_window_shift_once = False

    def on_drag_resize(self, msg: DragResize) -> None:
        if self._workspace_dynamic:
            return
        is_horizontal = (self.cfg.winsplitorientation == "horizontal")
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            return

        try:
            container = self.query_one("#split-container")
            origin_x = getattr(container.region, "x", 0)
            origin_y = getattr(container.region, "y", 0)
        except NoMatches:
            origin_x = 0
            origin_y = 0

        pos = (msg.screen_x - origin_x) if is_horizontal else (msg.screen_y - origin_y)
        pos = max(0, min(axis, int(pos)))
        self._cur_win_split = self._WIN_SPLIT_FREE
        self._window_shift = pos - (axis // 2)
        self._validate_window_shift(is_horizontal)
        self.cfg.winsplit = "free"
        self._apply_split()

    def on_resize(self, event: events.Resize) -> None:
        if self._workspace_dynamic:
            try:
                self.query_one("#split-container", PaneContainer).refresh(layout=True)
            except NoMatches:
                pass
            return
        self._apply_split()
        # GDBWidget.on_resize handles pyte + PTY resize itself via resize_gdb callback

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_status_file_info(self) -> None:
        src = self._get_source_view()
        if src is not None:
            src.refresh()

    def _sync_config(self) -> None:
        cfg = self.cfg
        src = self._get_source_view()
        if src is not None:
            old_tabstop = src.tabstop
            src.executing_line_display = cfg.executinglinedisplay
            src.selected_line_display = cfg.selectedlinedisplay
            src.tabstop = cfg.tabstop
            src.hlsearch = cfg.hlsearch
            src.ignorecase = cfg.ignorecase
            src.wrapscan = cfg.wrapscan
            src.showmarks = cfg.showmarks
            src.color = cfg.color
            # If tabstop changed, reload the current file so tabs are re-expanded
            # and the token cache is rebuilt with the new width.
            if cfg.tabstop != old_tabstop and src.source_file and src.source_file.path:
                src.load_file(src.source_file.path)
            else:
                src.refresh()
        gdb_w = self._get_gdb_widget()
        if gdb_w is not None:
            gdb_w.ignorecase = cfg.ignorecase
            gdb_w.wrapscan = cfg.wrapscan
            gdb_w.max_scrollback = cfg.scrollbackbuffersize
            gdb_w.debugwincolor = cfg.debugwincolor
            if gdb_w._screen:
                gdb_w._screen.use_color = cfg.debugwincolor
            gdb_w.refresh()
        self.km.timeout_ms = cfg.timeoutlen
        self.km.ttimeout_ms = cfg.ttimeoutlen
        self.km.timeout_enabled = cfg.timeout
        self.km.ttimeout_enabled = cfg.ttimeout
        if not self._workspace_dynamic and cfg.winsplitorientation != self._last_orientation:
            self._set_window_shift_from_ratio(
                cfg.winsplitorientation == "horizontal",
                self._split_ratio,
            )
            self._preserve_window_shift_once = True
        self._apply_split()

    def _show_help_in_source(self) -> None:
        help_candidates = [
            Path("/usr/share/cgdb/cgdb.txt"),
            Path(sys.prefix) / "share" / "cgdb" / "cgdb.txt",
            Path(__file__).resolve().parents[1] / "doc" / "cgdb.txt",
        ]
        src = self._get_source_view()
        if src is None:
            self._show_status("No source pane available")
            return
        for candidate in help_candidates:
            if candidate.is_file():
                if src.load_file(str(candidate)):
                    src.exe_line = 0
                    src.move_to(1)
                    self._switch_to_cgdb()
                    return

        lines = [
            "tgdb — Python reimplementation of cgdb",
            "",
            "CGDB mode (source window, press ESC):",
            "  j/k      down/up lines           G/gg  bottom/top",
            "  Ctrl-f/b page down/up             H/M/L screen positions",
            "  Ctrl-d/u half page down/up",
            "  /        search forward           ?    search backward",
            "  n/N      next/prev match",
            "  Space    toggle breakpoint        t    temporary breakpoint",
            "  o        open file dialog",
            "  m[a-z]   set local mark           '[a-z]  jump to mark",
            "  ''       last jump location       '.  executing line",
            "  Ctrl-W   toggle split orientation",
            "  -/=      shrink/grow source pane  _/+  by 25%",
            "  F5=run  F6=continue  F7=finish  F8=next  F10=step",
            "  i        switch to GDB mode",
            "  s        switch to GDB scroll mode",
            "  :        command mode",
            "",
            "GDB mode (GDB console, press i):",
            "  ESC      back to CGDB mode        PageUp  scroll mode",
            "  All keys forwarded to GDB (readline, history, etc.)",
            "",
            "Scroll mode (PageUp in GDB window):",
            "  j/k/PageUp/Dn  scroll             G/gg  end/beginning",
            "  //?/n/N  search                   q/i/Enter  exit scroll",
            "",
            "Commands (type : in CGDB mode):",
            "  :set tabstop=4          :set hlsearch",
            "  :set winsplit=even      :set executinglinedisplay=longarrow",
            "  :highlight Statement ctermfg=Yellow cterm=bold",
            "  :map <F8> :next<Enter>  :imap <F8> :next<Enter>",
            "  :break :continue :next :step :finish :run :quit",
            "  :shell [cmd]  run shell command    :capturescreen [file.svg]",
        ]
        sf = SourceFile("<help>", lines)
        src.source_file = sf
        src.exe_line = 0
        src.move_to(1)
        self._switch_to_cgdb()
