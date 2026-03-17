"""
GDB terminal widget — mirrors cgdb's scroller.cpp + vterminal.cpp.

Displays GDB console output with ANSI colour support.
Provides a scrollback buffer (configurable size, default 10000 lines).
Scroll mode: vi-like navigation, regex search (PageUp to enter).
Normal mode: keypresses forwarded directly to GDB PTY.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Callable, Optional

from textual.widget import Widget
from textual import events
from textual.message import Message
from rich.text import Text
from rich.style import Style
from rich.console import Console
import io

from .highlight_groups import HighlightGroups

# Module-level console for Text→Segment conversion (avoid per-line allocation)
_CONSOLE = Console(
    file=io.StringIO(), force_terminal=True,
    color_system="truecolor", width=300, highlight=False, markup=False
)

# ANSI SGR colour tables
_SGR_FG = {
    30: "black", 31: "red", 32: "green", 33: "yellow",
    34: "blue", 35: "magenta", 36: "cyan", 37: "white",
    90: "bright_black", 91: "bright_red", 92: "bright_green",
    93: "bright_yellow", 94: "bright_blue", 95: "bright_magenta",
    96: "bright_cyan", 97: "bright_white",
}
_SGR_BG = {k + 10: v for k, v in _SGR_FG.items()}


class GDBWidget(Widget):
    """
    GDB console widget with scrollback buffer and scroll mode.
    Normal mode: all keypresses go directly to GDB via PTY.
    Scroll mode: vi navigation + search (enter with PageUp, exit with q/i/Enter).
    """

    DEFAULT_CSS = """
    GDBWidget {
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups,
                 max_scrollback: int = 10000, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.max_scrollback = max_scrollback
        self.can_focus = True

        # Scrollback buffer of Rich Text lines
        self._lines: deque[Text] = deque(maxlen=max_scrollback)
        # Partial line being assembled
        self._partial: str = ""

        # Scroll state
        self._scroll_mode: bool = False
        self._scroll_top: int = 0      # 0-based first visible line index

        # Search state
        self._search_pattern: str = ""
        self._search_forward: bool = True
        self._search_active: bool = False
        self._search_buf: str = ""

        # Callbacks
        self.send_to_gdb: Callable[[str], None] = lambda s: None
        self.on_switch_to_cgdb: Callable[[], None] = lambda: None

        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self._num_buf: str = ""
        self._debug_color: bool = True

    # ------------------------------------------------------------------
    # Append GDB output
    # ------------------------------------------------------------------

    # Matches any ANSI/VT escape sequence (cursor movement, colours, etc.)
    _ANSI_STRIP_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def append_output(self, text: str) -> None:
        """Append raw text (may contain ANSI escapes) to the scrollback buffer."""
        # GDB console/prompt output always arrives as complete lines (newline
        # terminated). We reset _partial here so that user-typed readline echo
        # (managed by set_partial) is replaced when real output arrives.
        self._partial = ""
        self._partial += text
        while "\n" in self._partial:
            line_raw, self._partial = self._partial.split("\n", 1)
            self._lines.append(self._ansi_to_rich(line_raw))
        if not self._scroll_mode:
            self._scroll_to_end()
        self.refresh()

    def set_partial(self, raw: str) -> None:
        """Update the partial (current) line — used to show readline echo."""
        # Strip ANSI escape sequences that readline emits for cursor movement /
        # line-editing; we only want the visible characters.
        text = self._ANSI_STRIP_RE.sub("", raw).rstrip("\r")
        if text != self._partial:
            self._partial = text
            if not self._scroll_mode:
                self._scroll_to_end()
            self.refresh()

    def _ansi_to_rich(self, raw: str) -> Text:
        """Convert a raw line (possibly with ANSI SGR escapes) to Rich Text."""
        result = Text(no_wrap=True, overflow="crop")
        if "\x1b" not in raw:
            result.append(raw)
            return result

        _ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')
        pos = 0
        fg: Optional[str] = None
        bg: Optional[str] = None
        bold = italic = underline = False

        def cur_style() -> Style:
            return Style(color=fg, bgcolor=bg, bold=bold,
                         italic=italic, underline=underline)

        for m in _ANSI_RE.finditer(raw):
            if m.start() > pos:
                result.append(raw[pos:m.start()], style=cur_style())
            pos = m.end()
            params = [int(x) if x else 0 for x in m.group(1).split(";")]
            i = 0
            while i < len(params):
                p = params[i]
                if p == 0:
                    bold = italic = underline = False; fg = bg = None
                elif p == 1: bold = True
                elif p == 3: italic = True
                elif p == 4: underline = True
                elif p in _SGR_FG: fg = _SGR_FG[p]
                elif p in _SGR_BG: bg = _SGR_BG[p]
                elif p == 38 and i + 2 < len(params) and params[i+1] == 5:
                    fg = f"color({params[i+2]})"; i += 2
                elif p == 48 and i + 2 < len(params) and params[i+1] == 5:
                    bg = f"color({params[i+2]})"; i += 2
                i += 1
        if pos < len(raw):
            result.append(raw[pos:], style=cur_style())
        return result

    # ------------------------------------------------------------------
    # Scroll helpers
    # ------------------------------------------------------------------

    def _visible_height(self) -> int:
        return max(1, self.size.height)

    def _display_lines(self) -> list[Text]:
        lines = [line.copy() for line in self._lines]
        if self._partial:
            lines.append(self._ansi_to_rich(self._partial))
        return lines

    def _scroll_to_end(self) -> None:
        h = self._visible_height()
        n = len(self._display_lines())
        self._scroll_top = max(0, n - h)

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

    def _scroll_up(self, n: int = 1) -> None:
        self._scroll_top = max(0, self._scroll_top - n)
        self.refresh()

    def _scroll_down(self, n: int = 1) -> None:
        h = self._visible_height()
        total = len(self._lines)
        self._scroll_top = min(max(0, total - h), self._scroll_top + n)
        self.refresh()

    def _search(self, pattern: str, forward: bool) -> bool:
        lines = self._display_lines()
        total = len(lines)
        if not total or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        h = self._visible_height()
        cur = self._scroll_top
        if forward:
            order = list(range(cur + 1, total)) + (list(range(0, cur + 1)) if self.wrapscan else [])
        else:
            order = list(range(cur - 1, -1, -1)) + (list(range(total - 1, cur - 1, -1)) if self.wrapscan else [])
        for idx in order:
            if rx.search(lines[idx].plain):
                self._scroll_top = max(0, idx - h // 2)
                self.refresh()
                return True
        return False

    # ------------------------------------------------------------------
    # Rendering (render() only — no render_line override)
    # ------------------------------------------------------------------

    def render(self) -> Text:
        h = self._visible_height()
        lines_list = self._display_lines()
        total = len(lines_list)
        result = Text(no_wrap=True, overflow="crop")

        for y in range(h):
            idx = self._scroll_top + y
            if idx < total:
                line = lines_list[idx].copy()
                # Highlight search
                if self._search_pattern:
                    try:
                        flags = re.IGNORECASE if self.ignorecase else 0
                        rx = re.compile(self._search_pattern, flags)
                        for m in rx.finditer(line.plain):
                            line.stylize(self.hl.style("Search"),
                                         m.start(), m.end())
                    except re.error:
                        pass
                # Scroll mode status on last line
                if self._scroll_mode and y == h - 1:
                    line = self._build_scroll_status()
                result.append_text(line)
            else:
                if self._scroll_mode and y == h - 1:
                    result.append_text(self._build_scroll_status())
            if y < h - 1:
                result.append("\n")
        return result

    def _build_scroll_status(self) -> Text:
        total = len(self._display_lines())
        h = self._visible_height()
        bottom = min(total, self._scroll_top + h)
        pct = int(bottom * 100 / total) if total else 100
        style = self.hl.style("ScrollModeStatus")
        return Text(
            f" --scroll-- line {self._scroll_top + 1}/{total} ({pct}%)",
            style=style, no_wrap=True, overflow="crop"
        )

    def on_resize(self, event: events.Resize) -> None:
        if not self._scroll_mode:
            self._scroll_to_end()
        self.refresh()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    # Map Textual key names → bytes to send to GDB PTY
    _KEY_TO_GDB: dict[str, str] = {
        "enter":        "\r",
        "return":       "\r",
        "backspace":    "\x7f",
        "ctrl+h":       "\x08",
        "tab":          "\t",
        "ctrl+c":       "\x03",
        "ctrl+d":       "\x04",
        "ctrl+a":       "\x01",
        "ctrl+e":       "\x05",
        "ctrl+k":       "\x0b",
        "ctrl+u":       "\x15",
        "ctrl+w":       "\x17",
        "ctrl+l":       "\x0c",
        "up":           "\x1b[A",
        "down":         "\x1b[B",
        "right":        "\x1b[C",
        "left":         "\x1b[D",
        "home":         "\x1b[H",
        "end":          "\x1b[F",
        "pageup":       "\x1b[5~",
        "pagedown":     "\x1b[6~",
        "delete":       "\x1b[3~",
        "f1":  "\x1bOP",  "f2":  "\x1bOQ",  "f3":  "\x1bOR",  "f4":  "\x1bOS",
        "f5":  "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
        "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
    }

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        # Search input mode (scroll mode only)
        if self._search_active:
            self._handle_search_input(key, char)
            event.stop()
            return

        if self._scroll_mode:
            self._handle_scroll_key(key, char)
            event.stop()
            return

        # Normal mode — forward all keys to GDB PTY
        if key == "escape":
            self.on_switch_to_cgdb()
            event.stop()
            return
        if key == "pageup":
            self.enter_scroll_mode()
            self._scroll_up(self._visible_height())
            event.stop()
            return

        # Forward key to GDB
        raw = self._KEY_TO_GDB.get(key)
        if raw:
            self.send_to_gdb(raw)
        elif char and (char.isprintable() or char == "\t"):
            self.send_to_gdb(char)
        event.stop()

    def _handle_scroll_key(self, key: str, char: str) -> None:
        if char.isdigit() and char != "0":
            self._num_buf += char
            return
        count = int(self._num_buf) if self._num_buf else 1
        self._num_buf = ""

        if key == "escape":
            self.on_switch_to_cgdb()
            self.exit_scroll_mode()
        elif key in ("q", "i", "enter", "return"):
            self.exit_scroll_mode()
        elif key in ("j", "down", "ctrl+n"):
            self._scroll_down(count)
        elif key in ("k", "up", "ctrl+p"):
            self._scroll_up(count)
        elif key == "pageup":
            self._scroll_up(self._visible_height() * count)
        elif key == "pagedown":
            self._scroll_down(self._visible_height() * count)
        elif key == "ctrl+u":
            self._scroll_up(self._visible_height() // 2)
        elif key == "ctrl+d":
            self._scroll_down(self._visible_height() // 2)
        elif key in ("G", "end", "f12"):
            self._scroll_to_end(); self.refresh()
        elif key in ("home", "f11"):
            self._scroll_top = 0; self.refresh()
        elif char == "g":
            self._scroll_top = 0; self.refresh()
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
        elif char == "n":
            self._search(self._search_pattern, self._search_forward)
        elif char == "N":
            self._search(self._search_pattern, not self._search_forward)

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
