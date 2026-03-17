"""
GDB terminal widget — mirrors cgdb's scroller.cpp + vterminal.cpp.

Displays GDB console output with ANSI colour support.
Provides a scrollback buffer (configurable size).
Scroll mode: vi-like navigation, regex search.
Normal mode: passes keystrokes directly to GDB.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Optional, Callable

from textual.widget import Widget
from textual import events
from textual.message import Message
from rich.text import Text
from rich.style import Style

from .highlight_groups import HighlightGroups


# ---------------------------------------------------------------------------
# Scrollback buffer entry
# ---------------------------------------------------------------------------

class ScrollLine:
    __slots__ = ("text", "raw")

    def __init__(self, text: str, raw: str = "") -> None:
        self.text = text    # plain text content
        self.raw = raw      # original with ANSI escapes (for pyte rendering)


# ---------------------------------------------------------------------------
# GDB terminal widget
# ---------------------------------------------------------------------------

class GDBWidget(Widget):
    """
    Displays GDB console output.
    Two modes:
      - Normal (command) mode: keypresses go to GDB
      - Scroll mode: vi navigation, regex search
    """

    DEFAULT_CSS = """
    GDBWidget {
        height: 1fr;
        overflow: hidden;
        background: $surface;
    }
    """

    def __init__(self, hl: HighlightGroups,
                 max_scrollback: int = 10000, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.max_scrollback = max_scrollback
        self.can_focus = True

        # Scrollback buffer: deque of rendered Rich Text lines
        self._lines: deque[Text] = deque(maxlen=max_scrollback)
        # Current raw partial line being built
        self._partial: str = ""

        # Scroll state
        self._scroll_mode: bool = False
        self._scroll_top: int = 0      # index into _lines buffer
        self._search_pattern: str = ""
        self._search_forward: bool = True
        self._search_active: bool = False
        self._search_buf: str = ""

        # GDB input callback
        self.send_to_gdb: Callable[[str], None] = lambda s: None
        # Mode switch callback
        self.on_switch_to_cgdb: Callable[[], None] = lambda: None

        # Input line buffer (readline-like)
        self._input_buf: str = ""
        self._input_history: list[str] = []
        self._hist_idx: int = -1

        # Number prefix
        self._num_buf: str = ""

        self.ignorecase: bool = False
        self.wrapscan: bool = True

    # ------------------------------------------------------------------
    # Append output from GDB
    # ------------------------------------------------------------------

    def append_output(self, text: str, debug_color: bool = True) -> None:
        """Append text (possibly containing ANSI escapes) to the buffer."""
        self._partial += text
        # Split on newlines
        while "\n" in self._partial:
            line_raw, self._partial = self._partial.split("\n", 1)
            rich_line = self._ansi_to_rich(line_raw)
            self._lines.append(rich_line)
        if not self._scroll_mode:
            self._scroll_to_end()
        self.refresh()

    def _ansi_to_rich(self, raw: str) -> Text:
        """Convert a line with ANSI escapes to a Rich Text object."""
        result = Text(no_wrap=True, overflow="crop")
        # Fast path: strip ANSI and show plain if no escape sequences
        if "\x1b" not in raw:
            result.append(raw)
            return result
        # Parse ANSI SGR codes
        _ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')
        pos = 0
        current_style = Style()
        for m in _ANSI_RE.finditer(raw):
            # Append text before this escape
            if m.start() > pos:
                result.append(raw[pos:m.start()], style=current_style)
            pos = m.end()
            # Parse SGR parameters
            params = [int(x) if x else 0 for x in m.group(1).split(";")]
            current_style = self._sgr_to_style(params, current_style)
        if pos < len(raw):
            result.append(raw[pos:], style=current_style)
        return result

    _SGR_FG = {
        30: "black", 31: "red", 32: "green", 33: "yellow",
        34: "blue", 35: "magenta", 36: "cyan", 37: "white",
        90: "bright_black", 91: "bright_red", 92: "bright_green",
        93: "bright_yellow", 94: "bright_blue", 95: "bright_magenta",
        96: "bright_cyan", 97: "bright_white",
    }
    _SGR_BG = {k + 10: v for k, v in _SGR_FG.items()}

    def _sgr_to_style(self, params: list[int], current: Style) -> Style:
        bold = current.bold
        italic = current.italic
        underline = current.underline
        fg = current.color.name if current.color else None
        bg = current.bgcolor.name if current.bgcolor else None
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                bold = italic = underline = False
                fg = bg = None
            elif p == 1: bold = True
            elif p == 3: italic = True
            elif p == 4: underline = True
            elif p in self._SGR_FG: fg = self._SGR_FG[p]
            elif p in self._SGR_BG: bg = self._SGR_BG[p]
            elif p == 38 and i + 2 < len(params) and params[i+1] == 5:
                fg = f"color({params[i+2]})"
                i += 2
            elif p == 48 and i + 2 < len(params) and params[i+1] == 5:
                bg = f"color({params[i+2]})"
                i += 2
            i += 1
        return Style(bold=bold, italic=italic, underline=underline,
                     color=fg, bgcolor=bg)

    def _scroll_to_end(self) -> None:
        h = self._visible_height()
        n = len(self._lines)
        self._scroll_top = max(0, n - h)

    def _visible_height(self) -> int:
        return max(1, self.size.height)

    # ------------------------------------------------------------------
    # Scroll mode navigation
    # ------------------------------------------------------------------

    def enter_scroll_mode(self) -> None:
        self._scroll_mode = True
        self._scroll_to_end()
        self.post_message(ScrollModeChange(True))
        self.refresh()

    def exit_scroll_mode(self) -> None:
        self._scroll_mode = False
        self._scroll_to_end()
        self.post_message(ScrollModeChange(False))
        self.refresh()

    def _total_lines(self) -> int:
        return len(self._lines)

    def _scroll_up(self, n: int = 1) -> None:
        self._scroll_top = max(0, self._scroll_top - n)
        self.refresh()

    def _scroll_down(self, n: int = 1) -> None:
        h = self._visible_height()
        total = self._total_lines()
        self._scroll_top = min(max(0, total - h), self._scroll_top + n)
        self.refresh()

    def _scroll_page_up(self) -> None:
        self._scroll_up(self._visible_height())

    def _scroll_page_down(self) -> None:
        self._scroll_down(self._visible_height())

    def _goto_top(self) -> None:
        self._scroll_top = 0
        self.refresh()

    def _goto_bottom(self) -> None:
        self._scroll_to_end()
        self.refresh()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search(self, pattern: str, forward: bool,
                start: Optional[int] = None) -> bool:
        total = self._total_lines()
        if total == 0 or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False

        h = self._visible_height()
        cur = start if start is not None else self._scroll_top
        if forward:
            indices = list(range(cur + 1, total)) + (list(range(0, cur + 1)) if self.wrapscan else [])
        else:
            indices = list(range(cur - 1, -1, -1)) + (list(range(total - 1, cur - 1, -1)) if self.wrapscan else [])

        lines = list(self._lines)
        for idx in indices:
            if rx.search(lines[idx].plain):
                self._scroll_top = max(0, idx - h // 2)
                self.refresh()
                return True
        return False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _build_rich_line(self, y: int) -> Text:
        """Build Rich Text for one visible row."""
        total = len(self._lines)
        lines_list = list(self._lines)
        idx = self._scroll_top + y

        if idx >= total:
            return Text("")

        line = lines_list[idx].copy()
        # Highlight search matches
        if self._search_pattern:
            try:
                flags = re.IGNORECASE if self.ignorecase else 0
                rx = re.compile(self._search_pattern, flags)
                for m in rx.finditer(line.plain):
                    line.stylize(self.hl.style("Search"), m.start(), m.end())
            except re.error:
                pass
        return line

    def render_line(self, y: int) -> "Strip":
        from textual.strip import Strip
        from rich.console import Console
        h = self._visible_height()
        total = len(self._lines)
        width = self.size.width or 80

        # Last line: show scroll mode status if in scroll mode
        if self._scroll_mode and y == h - 1:
            text = self._build_scroll_status()
        else:
            text = self._build_rich_line(y)

        console = Console(width=width, highlight=False)
        segments = list(console.render(text, console.options.update_width(width)))
        segments = [s for s in segments if s.text != "\n"]
        return Strip(segments, width)

    def render(self) -> "Text":
        """Fallback full render."""
        h = self._visible_height()
        parts: list[Text] = []
        for y in range(h):
            if self._scroll_mode and y == h - 1:
                parts.append(self._build_scroll_status())
            else:
                parts.append(self._build_rich_line(y))
        result = Text()
        for i, p in enumerate(parts):
            result.append_text(p)
            if i < len(parts) - 1:
                result.append("\n")
        return result

    def _build_scroll_status(self) -> Text:
        total = self._total_lines()
        h = self._visible_height()
        bottom = min(total, self._scroll_top + h)
        pct = int(bottom * 100 / total) if total else 100
        style = self.hl.style("ScrollModeStatus")
        return Text(f" --scroll-- line {self._scroll_top + 1}/{total} ({pct}%)",
                    style=style, no_wrap=True, overflow="crop")

    def on_resize(self, event: events.Resize) -> None:
        if not self._scroll_mode:
            self._scroll_to_end()
        self.refresh()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if self._search_active:
            self._handle_search_input(key, char)
            event.stop()
            return

        if self._scroll_mode:
            self._handle_scroll_key(key, char)
            event.stop()
            return

        # Normal (command) mode — forward to GDB, except special keys
        if key == "escape":
            self.on_switch_to_cgdb()
            event.stop()
            return
        if key == "pageup":
            self.enter_scroll_mode()
            self._scroll_page_up()
            event.stop()
            return

        # Readline-like input handling
        if key == "enter":
            cmd = self._input_buf
            self._input_history.append(cmd)
            self._hist_idx = len(self._input_history)
            self._input_buf = ""
            self.send_to_gdb(cmd + "\n")
        elif key == "backspace":
            self._input_buf = self._input_buf[:-1]
        elif key == "up":
            if self._hist_idx > 0:
                self._hist_idx -= 1
                self._input_buf = self._input_history[self._hist_idx]
        elif key == "down":
            if self._hist_idx < len(self._input_history) - 1:
                self._hist_idx += 1
                self._input_buf = self._input_history[self._hist_idx]
            else:
                self._hist_idx = len(self._input_history)
                self._input_buf = ""
        elif key == "ctrl+c":
            self.send_to_gdb("\x03")
        elif key == "ctrl+d":
            self.send_to_gdb("\x04")
        elif char and char.isprintable():
            self._input_buf += char
        event.stop()

    def _handle_scroll_key(self, key: str, char: str) -> None:
        count = int(self._num_buf) if self._num_buf else 1
        self._num_buf = ""

        if char.isdigit() and key not in ("0",):
            self._num_buf += char
            return

        if key == "escape":
            self.on_switch_to_cgdb()
            self.exit_scroll_mode()
        elif key in ("q", "i", "enter"):
            self.exit_scroll_mode()
        elif key in ("j", "down", "ctrl+n"):
            self._scroll_down(count)
        elif key in ("k", "up", "ctrl+p"):
            self._scroll_up(count)
        elif key == "pageup":
            self._scroll_page_up()
        elif key == "pagedown":
            self._scroll_page_down()
        elif key == "ctrl+u":
            self._scroll_up(self._visible_height() // 2)
        elif key == "ctrl+d":
            self._scroll_down(self._visible_height() // 2)
        elif key in ("G", "end", "f12"):
            self._goto_bottom()
        elif key in ("home", "f11"):
            self._goto_top()
        elif key == "g":
            self._goto_top()
        elif key == "slash":
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
            self.post_message(ScrollSearchStart(True))
        elif key == "question_mark":
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
            self.post_message(ScrollSearchStart(False))
        elif key == "n":
            self._search(self._search_pattern, self._search_forward)
        elif key == "N":
            self._search(self._search_pattern, not self._search_forward)
        elif key == "apostrophe" and char == ".":
            self._goto_bottom()

    def _handle_search_input(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.post_message(ScrollSearchCancel())
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            if self._search_pattern:
                self._search(self._search_pattern, self._search_forward)
            self.post_message(ScrollSearchCommit(self._search_pattern))
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.post_message(ScrollSearchUpdate(self._search_buf))
        elif char and char.isprintable():
            self._search_buf += char
            self.post_message(ScrollSearchUpdate(self._search_buf))


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class ScrollModeChange(Message):
    def __init__(self, active: bool) -> None:
        super().__init__()
        self.active = active

class ScrollSearchStart(Message):
    def __init__(self, forward: bool) -> None:
        super().__init__()
        self.forward = forward

class ScrollSearchUpdate(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern

class ScrollSearchCommit(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern

class ScrollSearchCancel(Message):
    pass
