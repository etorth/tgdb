"""
Status bar widget — single row showing mode, filename, line number.
Also handles ':' command input and search prompts.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text
from typing import Callable, Optional

from .highlight_groups import HighlightGroups


class StatusBar(Widget):
    """One-row status bar. Renders a single Rich Text line."""

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

        # Normal: MODE  filename  line/total [*]
        mode_label = {
            "GDB":     "GDB",
            "CGDB":    "SRC",
            "SCROLL":  "SCROLL",
            "STATUS":  "CMD",
            "FILEDLG": "FILES",
        }.get(self._mode, self._mode)

        right = f" {self._lineno}/{self._total} "
        if self._mode == "GDB":
            right += "*"

        left   = f" {mode_label} "
        mid_w  = max(0, w - len(left) - len(right))
        fname  = self._filename
        if len(fname) > mid_w:
            fname = "…" + fname[-(mid_w - 1):]
        line = (left + fname.ljust(mid_w) + right)[:w].ljust(w)
        return Text(line, style=style, no_wrap=True, overflow="crop")

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


class CommandSubmit(Message):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

class CommandCancel(Message):
    pass
