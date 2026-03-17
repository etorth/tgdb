"""
Status bar widget — mirrors cgdb's status bar behaviour.

Shows:
  - Current mode (CGDB / GDB / SCROLL)
  - Filename and line:col
  - Search pattern while typing
  - Command input for ':' commands
  - Error messages
"""
from __future__ import annotations

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text
from typing import Callable, Optional

from .highlight_groups import HighlightGroups


class StatusBar(Widget):
    """Single-row status bar at the bottom of the source window."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: bottom;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._mode: str = "GDB"          # GDB | CGDB | SCROLL
        self._filename: str = ""
        self._lineno: int = 0
        self._total: int = 0
        self._message: str = ""          # Transient info/error message
        self._input_active: bool = False # ':' command input mode
        self._input_buf: str = ""        # Current ':' command being typed
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self.can_focus = True
        self.on_command: Callable[[str], None] = lambda s: None

    # ------------------------------------------------------------------
    # State setters
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._message = ""
        self.refresh()

    def set_file_info(self, filename: str, lineno: int, total: int) -> None:
        self._filename = filename
        self._lineno = lineno
        self._total = total
        self.refresh()

    def show_message(self, msg: str) -> None:
        self._message = msg
        self.refresh()

    def start_command(self) -> None:
        """Enter ':' command mode."""
        self._input_active = True
        self._search_active = False
        self._input_buf = ""
        self.refresh()

    def start_search(self, forward: bool) -> None:
        self._search_active = True
        self._search_forward = forward
        self._search_buf = ""
        self._input_active = False
        self.refresh()

    def update_search(self, pattern: str) -> None:
        self._search_buf = pattern
        self.refresh()

    def cancel_input(self) -> None:
        self._input_active = False
        self._search_active = False
        self._message = ""
        self.refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w = self.size.width or 80
        style = self.hl.style("StatusLine")

        if self._input_active:
            text = Text(f":{self._input_buf}", style=style,
                        no_wrap=True, overflow="crop")
            text.pad_right(w)
            return text

        if self._search_active:
            prefix = "/" if self._search_forward else "?"
            text = Text(f"{prefix}{self._search_buf}", style=style,
                        no_wrap=True, overflow="crop")
            text.pad_right(w)
            return text

        if self._message:
            text = Text(self._message, style=style,
                        no_wrap=True, overflow="crop")
            text.pad_right(w)
            return text

        # Normal status line:  MODE  filename  line/total *
        mode_indicator = {
            "GDB": "GDB",
            "CGDB": "SRC",
            "SCROLL": "SCROLL",
            "FILEDLG": "FILES",
        }.get(self._mode, self._mode)

        right = f" {self._lineno}/{self._total} "
        if self._mode == "GDB":
            right += "*"

        fname = self._filename
        mid_max = w - len(mode_indicator) - 3 - len(right)
        if len(fname) > mid_max:
            fname = "…" + fname[-(mid_max - 1):]

        left = f" {mode_indicator} "
        middle = fname.ljust(mid_max)
        line = (left + middle + right)[:w]
        line = line.ljust(w)
        return Text(line, style=style, no_wrap=True, overflow="crop")

    # ------------------------------------------------------------------
    # Key handling (command input mode)
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if not self._input_active:
            return
        key = event.key
        char = event.character or ""

        if key == "escape":
            self._input_active = False
            self._message = ""
            self.post_message(CommandCancel())
            self.refresh()
        elif key in ("enter", "return"):
            cmd = self._input_buf
            self._input_active = False
            self._input_buf = ""
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


# Messages

class CommandSubmit(Message):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

class CommandCancel(Message):
    pass
