"""
Dedicated bottom status bar for ':' commands, search prompts, and messages.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text

from .highlight_groups import HighlightGroups


class StatusBar(Widget):
    """One-row status bar at the bottom of the screen."""

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

    def feed_key(self, key: str, char: str) -> bool:
        """Handle command-bar input even if focus hasn't switched yet."""
        if not self._input_active:
            return False

        if key == "escape":
            self._input_active = False
            self.post_message(CommandCancel())
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
        else:
            return False
        return True

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

        return Text(" " * w, style=style, no_wrap=True, overflow="crop")

    # ------------------------------------------------------------------
    # Key handling — only active during command input
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self.feed_key(event.key, event.character or ""):
            event.stop()


class CommandSubmit(Message):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

class CommandCancel(Message):
    pass
