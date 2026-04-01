"""
pyte terminal emulation helpers for the GDB console widget.

Colour conversion (pyte → Rich), row-to-Text rendering, and a custom
pyte.Screen subclass that captures lines scrolling off the top into a
scrollback buffer.
"""

from __future__ import annotations

from typing import Callable, Optional

import pyte
from rich.text import Text
from rich.style import Style


# ---------------------------------------------------------------------------
# pyte colour → Rich colour conversion
# ---------------------------------------------------------------------------

_PYTE_NAMED = {
    "black": "black",
    "red": "red",
    "green": "green",
    "brown": "yellow",
    "blue": "blue",
    "magenta": "magenta",
    "cyan": "cyan",
    "white": "white",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightyellow": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}


def _pyte_color(c: str) -> Optional[str]:
    if not c or c == "default":
        return None
    if c in _PYTE_NAMED:
        return _PYTE_NAMED[c]
    if c.isdigit():
        return f"color({c})"
    if ";" in c:  # truecolor r;g;b
        parts = c.split(";")
        if len(parts) == 3:
            try:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                return f"#{r:02x}{g:02x}{b:02x}"
            except ValueError:
                pass
    return None


def _row_to_text(row, width: int, cursor_col: int = -1, use_color: bool = True) -> Text:
    """Convert one pyte screen row to a Rich Text line.

    ``row`` may be None (row was never written to in pyte's buffer).
    Use ``screen.buffer.get(r)`` instead of ``screen.buffer[r]`` at
    call sites to avoid creating phantom empty entries in the
    defaultdict, which confuses pyte's delete_lines logic on resize.
    """
    result = Text(no_wrap=True, overflow="crop")
    for col in range(width):
        if row is None:
            data = " "
            st = Style(reverse=True, blink=True) if col == cursor_col else Style()
        else:
            # Use .get() rather than row[col] so that both pyte's
            # StaticDefaultDict rows AND plain dict rows (restored from
            # _scrollback_raw) work correctly.  StaticDefaultDict.__missing__
            # is only triggered by [] syntax, not .get(), so .get() returns
            # None for a missing column in either dict type — we treat that as
            # a blank cell, which is the correct fallback.
            char = row.get(col)
            if char is None:
                data = " "
                st = Style(reverse=True, blink=True) if col == cursor_col else Style()
                result.append(data, style=st)
                continue
            data = char.data or " "
            if use_color:
                fg = _pyte_color(char.fg)
                bg = _pyte_color(char.bg)
                if char.reverse:
                    fg, bg = bg, fg
                st = Style(
                    color=fg,
                    bgcolor=bg,
                    bold=char.bold,
                    italic=char.italics,
                    underline=char.underscore,
                    blink=char.blink,
                )
            else:
                st = Style(
                    bold=char.bold,
                    italic=char.italics,
                    underline=char.underscore,
                    blink=char.blink,
                    reverse=char.reverse,
                )
            if col == cursor_col:
                st = st + Style(reverse=True, blink=True)
        result.append(data, style=st)
    return result


# ---------------------------------------------------------------------------
# pyte.Screen subclass that captures lines scrolling off the top
# ---------------------------------------------------------------------------


class _GDBScreen(pyte.Screen):
    """Intercept index() to save lines that scroll off into a deque."""

    def __init__(self, columns: int, lines: int, push_fn: "Callable[[Text, Optional[dict]], None]") -> None:
        super().__init__(columns, lines)
        self._push_scrollback = push_fn
        self.use_color: bool = True  # set by GDBWidget to honour debugwincolor

    def index(self) -> None:
        if self.cursor.y == self.lines - 1:
            # Row 0 is about to be lost — capture both the rendered text and
            # the raw pyte row dict so the row can be restored if the pane
            # grows back later.  Use .get(0) to avoid phantom defaultdict entries.
            row = self.buffer.get(0)
            text = _row_to_text(row, self.columns, use_color=self.use_color)
            # Save the row reference directly — no dict() copy needed.
            # super().index() shifts the buffer and removes row 0, so after
            # this call pyte no longer holds a reference to this object.
            # _scrollback_raw becomes the sole owner; pyte cannot mutate it.
            self._push_scrollback(text, row)
        super().index()
