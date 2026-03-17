"""
Status bar widget — single row showing mode, filename, line number.
Also handles ':' command input and search prompts.
Mouse press+drag resizes the src/gdb panes.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text
from typing import Callable, Optional

from .highlight_groups import HighlightGroups


class StatusBar(Widget):
    """One-row status bar between source and GDB panes.
    Drag vertically to resize the split."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary-darken-2;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._mode: str = "GDB"
        self._filename: str = ""
        self._lineno: int = 0
        self._total: int = 0
        self._message: str = ""
        self._input_active: bool = False
        self._input_buf: str = ""
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self.can_focus = True
        # Resize drag state
        self._dragging: bool = False
        self.drag_enabled: bool = True   # disabled in vertical split

    # ------------------------------------------------------------------
    # State setters (called by app)
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._message = ""
        self.refresh()

    def set_file_info(self, filename: str, lineno: int, total: int) -> None:
        self._filename = filename
        self._lineno   = lineno
        self._total    = total
        self.refresh()

    def show_message(self, msg: str) -> None:
        self._message = msg
        self.refresh()

    def start_command(self) -> None:
        self._input_active  = True
        self._search_active = False
        self._input_buf     = ""
        self.refresh()

    def start_search(self, forward: bool) -> None:
        self._search_active  = True
        self._search_forward = forward
        self._search_buf     = ""
        self._input_active   = False
        self.refresh()

    def update_search(self, pattern: str) -> None:
        self._search_buf = pattern
        self.refresh()

    def cancel_input(self) -> None:
        self._input_active  = False
        self._search_active = False
        self._message       = ""
        self.refresh()

    # ------------------------------------------------------------------
    # Rendering — returns a single-line Rich Text
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w = max(10, self.size.width or 80)
        style = self.hl.style("StatusLine")

        if self._input_active:
            t = Text(f":{self._input_buf}", no_wrap=True, overflow="crop")
            t.pad_right(w - len(t.plain))
            t.stylize(style)
            return t

        if self._search_active:
            pfx = "/" if self._search_forward else "?"
            t = Text(f"{pfx}{self._search_buf}", no_wrap=True, overflow="crop")
            t.pad_right(w - len(t.plain))
            t.stylize(style)
            return t

        if self._message:
            t = Text(self._message[:w].ljust(w), style=style,
                     no_wrap=True, overflow="crop")
            return t

        # Normal: filename left-aligned, '*' at right edge when GDB focused
        # Matches cgdb update_status_win(): if_display_message("", filename)
        # with '*' appended at WIDTH-1 when focus==GDB.
        fname = self._filename
        star  = "*" if self._mode == "GDB" else " "
        avail = max(0, w - 1)  # leave last column for star
        if len(fname) > avail:
            fname = "…" + fname[-(avail - 1):]
        line = fname.ljust(avail) + star
        return Text(line[:w], style=style, no_wrap=True, overflow="crop")

    # ------------------------------------------------------------------
    # Key handling — only active during command input
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if not self._input_active:
            return
        key  = event.key
        char = event.character or ""

        if key == "escape":
            self._input_active = False
            self.post_message(CommandCancel())
        elif key in ("enter", "return"):
            cmd = self._input_buf
            self._input_active = False
            self._input_buf    = ""
            self.refresh()
            self.post_message(CommandSubmit(cmd))
        elif key in ("backspace", "ctrl+h"):
            self._input_buf = self._input_buf[:-1]
            if not self._input_buf:
                self._input_active = False
                self.post_message(CommandCancel())
            self.refresh()
        elif char and char.isprintable():
            self._input_buf += char
            self.refresh()
        event.stop()


    # ------------------------------------------------------------------
    # Mouse drag — resize src/gdb panes by dragging the status bar
    # ------------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1 and self.drag_enabled:
            self._dragging = True
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            self.post_message(DragResize(int(event.screen_y)))
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging and event.button == 1:
            self._dragging = False
            self.release_mouse()
            event.stop()


class CommandSubmit(Message):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

class CommandCancel(Message):
    pass

class DragResize(Message):
    """Posted while the user drags a splitter bar.
    screen_y: used when dragging status bar (horizontal split)
    screen_x: used when dragging vsep (vertical split)
    """
    def __init__(self, screen_y: int = 0, screen_x: int = 0) -> None:
        super().__init__()
        self.screen_y = screen_y
        self.screen_x = screen_x
