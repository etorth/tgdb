"""
GDB console widget — mirrors cgdb's scroller.cpp / vterminal.cpp.

cgdb uses libvterm (VT100 terminal emulator) for the bottom pane.
We use pyte, which provides the same VT100 emulation in Python.

The widget is a real terminal connected to GDB's PTY master:
  • feed_bytes(data) drives the pyte screen with raw PTY bytes
  • render() reads from the pyte screen (colours, cursor, etc.)
  • on_key() writes raw bytes directly to GDB's PTY stdin

Scroll mode (PageUp): vi-like navigation + regex search over
a scrollback buffer of lines captured as they scroll off the screen.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Callable, Optional

import pyte
from textual.widget import Widget
from textual import events
from textual.message import Message
from rich.text import Text
from rich.style import Style

from .highlight_groups import HighlightGroups


# ---------------------------------------------------------------------------
# pyte colour → Rich colour conversion
# ---------------------------------------------------------------------------

_PYTE_NAMED = {
    "black":        "black",
    "red":          "red",
    "green":        "green",
    "brown":        "yellow",
    "blue":         "blue",
    "magenta":      "magenta",
    "cyan":         "cyan",
    "white":        "white",
    "brightblack":  "bright_black",
    "brightred":    "bright_red",
    "brightgreen":  "bright_green",
    "brightyellow": "bright_yellow",
    "brightblue":   "bright_blue",
    "brightmagenta":"bright_magenta",
    "brightcyan":   "bright_cyan",
    "brightwhite":  "bright_white",
}


def _pyte_color(c: str) -> Optional[str]:
    if not c or c == "default":
        return None
    if c in _PYTE_NAMED:
        return _PYTE_NAMED[c]
    if c.isdigit():
        return f"color({c})"
    if ";" in c:           # truecolor r;g;b
        parts = c.split(";")
        if len(parts) == 3:
            try:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                return f"#{r:02x}{g:02x}{b:02x}"
            except ValueError:
                pass
    return None


def _row_to_text(row, width: int, cursor_col: int = -1,
                 use_color: bool = True) -> Text:
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
            char = row[col]          # StaticDefaultDict — never inserts on access
            data = char.data or " "
            if use_color:
                fg   = _pyte_color(char.fg)
                bg   = _pyte_color(char.bg)
                if char.reverse:
                    fg, bg = bg, fg
                st = Style(
                    color=fg, bgcolor=bg,
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

    def __init__(self, columns: int, lines: int,
                 push_fn: "Callable[[Text, Optional[dict]], None]") -> None:
        super().__init__(columns, lines)
        self._push_scrollback = push_fn
        self.use_color: bool = True   # set by GDBWidget to honour debugwincolor

    def index(self) -> None:
        if self.cursor.y == self.lines - 1:
            # Row 0 is about to be lost — capture both the rendered text and
            # the raw pyte row dict so the row can be restored if the pane
            # grows back later.  Use .get(0) to avoid phantom defaultdict entries.
            row  = self.buffer.get(0)
            text = _row_to_text(row, self.columns, use_color=self.use_color)
            raw  = dict(row) if row is not None else None
            self._push_scrollback(text, raw)
        super().index()


# ---------------------------------------------------------------------------
# GDB widget
# ---------------------------------------------------------------------------

class GDBWidget(Widget):
    """
    Bottom pane: real VT100 terminal emulator (pyte) connected to GDB's PTY.
    Mirrors cgdb's scroller which uses libvterm.
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
        # cgdb: scr_refresh(gdb_scroller, focus==GDB, ...) — cursor only shown when GDB focused
        self.gdb_focused: bool = True

        # Scrollback: lines captured as they scroll off the pyte screen.
        # _scrollback stores Rich Text (for display/search).
        # _scrollback_raw stores the raw pyte row dict in parallel so rows can
        # be restored back into the pyte buffer when the pane grows.
        self._scrollback:     deque[Text] = deque(maxlen=max_scrollback)
        self._scrollback_raw: deque       = deque(maxlen=max_scrollback)
        self.debugwincolor: bool = True  # :set debugwincolor — show ANSI colors
        # pyte terminal (lazily resized to match widget)
        self._screen: Optional[_GDBScreen] = None
        self._stream: Optional[pyte.ByteStream] = None
        self._pyte_rows: int = 24
        self._pyte_cols: int = 80
        self._init_pyte(24, 80)

        # Scroll mode
        self._scroll_mode:   bool = False
        self._scroll_offset: int  = 0   # lines above live bottom (0 = live)
        self._h_offset:      int  = 0   # horizontal scroll (cgdb scroll_cursor_col)

        # Search (scroll mode only)
        self._search_pattern: str  = ""
        self._search_forward: bool = True
        self._search_active:  bool = False
        self._search_buf:     str  = ""

        # Callbacks set by app
        self.send_to_gdb:        Callable[[bytes], None]  = lambda b: None
        self.resize_gdb:         Callable[[int, int], None] = lambda r, c: None
        self.on_switch_to_cgdb:  Callable[[], None]       = lambda: None

        self.ignorecase: bool = False
        self.wrapscan:   bool = True
        self._num_buf:   str  = ""
        self._await_g:   bool = False   # true after first 'g' (for 'gg')
        self._dot_pending: bool = False  # true after apostrophe (for `'.`)

    # ------------------------------------------------------------------
    # Scrollback helpers
    # ------------------------------------------------------------------

    def _push_to_scrollback(self, text: Text, raw=None) -> None:
        """Append one line to both scrollback deques (text for display, raw for restoration)."""
        self._scrollback.append(text)
        self._scrollback_raw.append(raw)   # dict copy of pyte row, or None

    # ------------------------------------------------------------------
    # pyte initialisation / resize
    # ------------------------------------------------------------------

    def _init_pyte(self, rows: int, cols: int) -> None:
        """Create or resize the pyte terminal, preserving existing content."""
        if self._screen is None:
            # First init — create fresh screen
            self._pyte_rows = rows
            self._pyte_cols = cols
            self._screen = _GDBScreen(cols, rows, self._push_to_scrollback)
            self._screen.use_color = self.debugwincolor
            self._stream = pyte.ByteStream(self._screen)
        else:
            if rows < self._pyte_rows:
                # pyte.Screen.resize() calls delete_lines(old_rows - new_rows)
                # with the cursor moved to row 0.  This always drops
                # (old_rows - new_rows) rows from the top regardless of where
                # the cursor actually is — which destroys content when the
                # pane shrinks but the cursor is not near the bottom.
                #
                # The correct behaviour (matching cgdb/libvterm) is:
                #   only push as many rows off the top as needed to keep the
                #   cursor on screen: top_scroll = max(0, cursor.y+1 - new_rows)
                #
                # We implement this ourselves and then tell pyte the new line
                # count directly so its resize() skips delete_lines entirely.
                cy = self._screen.cursor.y
                buf = self._screen.buffer
                use_color = self._screen.use_color

                top_scroll = max(0, cy + 1 - rows)

                # Push displaced rows to both scrollback deques (text + raw copy).
                for r in range(top_scroll):
                    row = buf.get(r)
                    self._push_to_scrollback(
                        _row_to_text(row, self._pyte_cols, use_color=use_color),
                        dict(row) if row is not None else None,
                    )

                # Shift the buffer up by top_scroll rows in-place.
                new_entries: dict = {}
                for old_r in list(buf.keys()):
                    new_r = old_r - top_scroll
                    if 0 <= new_r < rows:
                        new_entries[new_r] = buf[old_r]
                buf.clear()
                buf.update(new_entries)

                # Clamp cursor.
                self._screen.cursor.y = max(0, cy - top_scroll)

                # Tell pyte the screen is already at the new row count so
                # its resize() sees lines == self.lines and skips delete_lines.
                self._screen.lines = rows
                self._screen.dirty.update(range(rows))

            elif rows > self._pyte_rows:
                # Growing: pull rows back from scrollback to fill the newly
                # visible space at the top — mirrors cgdb/libvterm grow behaviour.
                grow      = rows - self._pyte_rows
                n_restore = min(grow, len(self._scrollback_raw))

                if n_restore > 0:
                    buf = self._screen.buffer

                    # Pop from the RIGHT (most recently pushed = was topmost row
                    # just before the last shrink).  Collect in push order so
                    # the oldest of the restored set goes to row 0 and the
                    # most-recent goes to row n_restore-1 (just above content).
                    to_restore: list[Optional[dict]] = []
                    for _ in range(n_restore):
                        self._scrollback.pop()           # keep display deque in sync
                        to_restore.append(self._scrollback_raw.pop())
                    # to_restore[0] = most recently pushed → row n_restore-1
                    # to_restore[-1] = oldest restored    → row 0
                    # reversed() puts them in correct top-to-bottom order.
                    ordered = list(reversed(to_restore))

                    # Shift existing buffer content down by n_restore.
                    new_entries = {}
                    for old_r in list(buf.keys()):
                        new_entries[old_r + n_restore] = buf[old_r]
                    buf.clear()
                    buf.update(new_entries)

                    # Place restored rows at the top.
                    for i, raw in enumerate(ordered):
                        if raw is not None:
                            buf[i] = raw
                        # else: leave the slot blank (not in buffer)

                    # Shift cursor down to match the shifted content.
                    cy = self._screen.cursor.y
                    self._screen.cursor.y = min(rows - 1, cy + n_restore)
                    self._screen.dirty.update(range(rows))

            self._pyte_rows = rows
            self._pyte_cols = cols
            # Let pyte handle column changes (and the dirty/margin bookkeeping).
            # For shrink: screen.lines was pre-set above, so pyte skips delete_lines.
            # For grow:   pyte just extends self.lines and updates self.columns.
            self._screen.resize(rows, cols)

    # ------------------------------------------------------------------
    # Feed raw GDB console bytes into the pyte emulator
    # ------------------------------------------------------------------

    def feed_bytes(self, data: bytes) -> None:
        """
        Called with raw bytes from GDB's primary PTY.
        pyte interprets VT100 escape sequences, updates screen state.
        """
        if self._stream:
            self._stream.feed(data)
        if not self._scroll_mode:
            self.refresh()

    def inject_text(self, text: str) -> None:
        """Inject plain text directly into the scrollback (showdebugcommands)."""
        self._push_to_scrollback(Text(text.rstrip("\n")), None)
        if not self._scroll_mode:
            self.refresh()

    def _at_empty_gdb_prompt(self) -> bool:
        if not self._screen:
            return False

        cy = self._screen.cursor.y
        row = _row_to_text(self._screen.buffer[cy], self._pyte_cols, use_color=False).plain
        before_cursor = row[: self._screen.cursor.x]
        return before_cursor.endswith("(gdb) ")

    def _maybe_escape_burst_key(self, key: str, char: str) -> Optional[tuple[str, str]]:
        if key.startswith("alt+") and char and char.isprintable():
            return char, char

        if self._at_empty_gdb_prompt() and key in ("colon", "slash", "question_mark"):
            return key, char

        return None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _visible_height(self) -> int:
        return max(1, self.size.height)

    def _all_lines(self) -> list[Text]:
        """scrollback + current pyte screen rows as Text list."""
        lines = list(self._scrollback)
        if self._screen:
            cx = self._screen.cursor.x
            cy = self._screen.cursor.y
            buf = self._screen.buffer
            for r in range(self._pyte_rows):
                cursor_col = cx if (r == cy and not self._scroll_mode and self.gdb_focused) else -1
                lines.append(_row_to_text(buf.get(r),
                                          self._pyte_cols, cursor_col,
                                          use_color=self.debugwincolor))
        return lines

    def render(self) -> Text:
        h = self._visible_height()
        if self._scroll_mode:
            return self._render_scroll(h)
        return self._render_live(h)

    def _render_live(self, h: int) -> Text:
        """Render the live pyte screen (normal mode)."""
        result = Text(no_wrap=True, overflow="crop")
        if not self._screen:
            return result
        cx = self._screen.cursor.x
        cy = self._screen.cursor.y
        buf = self._screen.buffer
        for r in range(min(h, self._pyte_rows)):
            if r > 0:
                result.append("\n")
            cursor_col = cx if (r == cy and self.gdb_focused) else -1
            # Use buf.get(r) — NOT buf[r] — to avoid creating phantom empty
            # entries in pyte's defaultdict buffer.  Phantom entries confuse
            # pyte's delete_lines logic on the next resize: it sees them as
            # "content" and displaces real rows, clearing the screen.
            result.append_text(
                _row_to_text(buf.get(r), self._pyte_cols, cursor_col,
                             use_color=self.debugwincolor)
            )
        # Pad remaining rows if widget is taller than pyte screen
        for r in range(self._pyte_rows, h):
            result.append("\n")
        return result

    def _render_scroll(self, h: int) -> Text:
        """Render scroll mode: combined scrollback + screen."""
        lines = self._all_lines()
        total = len(lines)
        start = max(0, total - h - self._scroll_offset)
        w = self.size.width or 80
        rendered_lines: list[Text] = []
        for y in range(h):
            idx = start + y
            if idx < total:
                line = lines[idx].copy()
                if self._search_pattern:
                    try:
                        flags = re.IGNORECASE if self.ignorecase else 0
                        rx = re.compile(self._search_pattern, flags)
                        for m in rx.finditer(line.plain):
                            line.stylize(self.hl.style("Search"),
                                         m.start(), m.end())
                    except re.error:
                        pass
                # Apply horizontal offset (cgdb scroll_cursor_col)
                if self._h_offset > 0:
                    # Trim _h_offset chars from the left
                    plain = line.plain
                    if self._h_offset < len(plain):
                        line = line[self._h_offset:]
                    else:
                        line = Text()
                rendered_lines.append(line)
            else:
                rendered_lines.append(Text())

        # Mirror cgdb scroller.cpp: overlay "[delta/total]" on the top-right.
        if rendered_lines:
            delta = self._scroll_offset
            sb_total = len(self._scrollback)
            stat = f"[{delta}/{sb_total}]"
            first = rendered_lines[0]
            left_width = max(0, w - len(stat))
            rendered_lines[0] = Text.assemble(
                first[:left_width],
                (stat, self.hl.style("ScrollModeStatus")),
            )

        out = Text(no_wrap=True, overflow="crop")
        for i, line in enumerate(rendered_lines):
            if i > 0:
                out.append("\n")
            out.append_text(line)
        return out

    # ------------------------------------------------------------------
    # Resize — keep pyte screen in sync with widget size
    # ------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        new_rows = event.size.height
        new_cols = event.size.width
        if new_rows != self._pyte_rows or new_cols != self._pyte_cols:
            self._init_pyte(new_rows, new_cols)
            self.resize_gdb(new_rows, new_cols)
        self.refresh()

    # ------------------------------------------------------------------
    # Scroll helpers
    # ------------------------------------------------------------------

    def enter_scroll_mode(self) -> None:
        self._scroll_mode   = True
        self._scroll_offset = 0
        self.post_message(ScrollModeChange(True))
        self.refresh()

    def exit_scroll_mode(self) -> None:
        self._scroll_mode   = False
        self._scroll_offset = 0
        self._h_offset      = 0
        self.post_message(ScrollModeChange(False))
        self.refresh()

    def _scroll_up(self, n: int = 1) -> None:
        max_off = len(self._scrollback)
        self._scroll_offset = min(max_off, self._scroll_offset + n)
        self.refresh()

    def _scroll_down(self, n: int = 1) -> None:
        self._scroll_offset = max(0, self._scroll_offset - n)
        self.refresh()

    def _scroll_left(self) -> None:
        """Horizontal scroll left (cgdb scr_left)."""
        if self._h_offset > 0:
            self._h_offset -= 1
            self.refresh()

    def _scroll_right(self) -> None:
        """Horizontal scroll right (cgdb scr_right)."""
        self._h_offset += 1
        self.refresh()

    def _beginning_of_row(self) -> None:
        """Jump to start of row (cgdb scr_beginning_of_row, key '0')."""
        self._h_offset = 0
        self.refresh()

    def _end_of_row(self) -> None:
        """Jump to end of row (cgdb scr_end_of_row, key '$')."""
        # Measure longest visible line
        lines = self._all_lines()
        total = len(lines)
        h = self._visible_height()
        start = max(0, total - h - self._scroll_offset)
        w = max(80, self.size.width or 80)
        max_w = max((len(lines[i].plain) for i in range(start, min(start + h, total))), default=w)
        self._h_offset = max(0, max_w - w)
        self.refresh()

    def _do_search(self, pattern: str, forward: bool) -> bool:
        lines = self._all_lines()
        total = len(lines)
        if not total or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        h   = self._visible_height()
        cur = max(0, total - h - self._scroll_offset)
        order = (
            list(range(cur + 1, total)) +
            (list(range(0, cur + 1)) if self.wrapscan else [])
        ) if forward else (
            list(range(cur - 1, -1, -1)) +
            (list(range(total - 1, cur - 1, -1)) if self.wrapscan else [])
        )
        for idx in order:
            if rx.search(lines[idx].plain):
                self._scroll_offset = max(0, total - h - idx)
                self.refresh()
                return True
        return False

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    # Textual key name → raw bytes to write to GDB's PTY
    _KEY_BYTES: dict[str, bytes] = {
        "enter":      b"\r",
        "return":     b"\r",
        "backspace":  b"\x7f",
        "ctrl+h":     b"\x08",
        "tab":        b"\t",
        "ctrl+c":     b"\x03",
        "ctrl+d":     b"\x04",
        "ctrl+a":     b"\x01",
        "ctrl+b":     b"\x02",
        "ctrl+e":     b"\x05",
        "ctrl+f":     b"\x06",
        "ctrl+k":     b"\x0b",
        "ctrl+n":     b"\x0e",
        "ctrl+p":     b"\x10",
        "ctrl+r":     b"\x12",
        "ctrl+u":     b"\x15",
        "ctrl+w":     b"\x17",
        "ctrl+l":     b"\x0c",
        "escape":     b"\x1b",
        "up":         b"\x1b[A",
        "down":       b"\x1b[B",
        "right":      b"\x1b[C",
        "left":       b"\x1b[D",
        "home":       b"\x1b[H",
        "end":        b"\x1b[F",
        "pageup":     b"\x1b[5~",
        "pagedown":   b"\x1b[6~",
        "delete":     b"\x1b[3~",
        "f1":  b"\x1bOP",   "f2":  b"\x1bOQ",
        "f3":  b"\x1bOR",   "f4":  b"\x1bOS",
        "f5":  b"\x1b[15~", "f6":  b"\x1b[17~",
        "f7":  b"\x1b[18~", "f8":  b"\x1b[19~",
        "f10": b"\x1b[21~", "f11": b"\x1b[23~",
        "f12": b"\x1b[24~",
    }

    def on_key(self, event: events.Key) -> None:
        key  = event.key
        char = event.character or ""

        # Search input (scroll mode only)
        if self._search_active:
            self._handle_search_key(key, char)
            event.stop()
            return

        if self._scroll_mode:
            self._handle_scroll_key(key, char)
            event.stop()
            return

        # If focus is still on the GDB pane after switching modes, absorb and
        # reroute the key instead of leaking it into the GDB PTY.
        handler = getattr(self.app, "_handle_non_gdb_focus_key", None)
        if callable(handler) and handler(key, char):
            event.stop()
            return

        burst_key = self._maybe_escape_burst_key(key, char)
        if burst_key is not None:
            self.on_switch_to_cgdb()
            if callable(handler):
                handler(*burst_key)
            event.stop()
            return

        # Normal mode — ESC switches to CGDB source pane
        if key == "escape":
            self.on_switch_to_cgdb()
            event.stop()
            return
        # PageUp enters scroll mode
        if key == "pageup":
            self.enter_scroll_mode()
            self._scroll_up(self._visible_height())
            event.stop()
            return

        # All other keys forwarded directly to GDB's PTY
        raw = self._KEY_BYTES.get(key)
        if raw:
            self.send_to_gdb(raw)
        elif char and (char.isprintable() or char == "\t"):
            self.send_to_gdb(char.encode())
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
            self._await_g = False; self._scroll_down(count)
        elif key in ("k", "up", "ctrl+p"):
            self._await_g = False; self._scroll_up(count)
        elif key in ("h", "left"):
            self._await_g = False; self._scroll_left()
        elif key in ("l", "right"):
            self._await_g = False; self._scroll_right()
        elif char == "0":
            self._await_g = False; self._beginning_of_row()
        elif char == "$":
            self._await_g = False; self._end_of_row()
        elif key in ("pageup", "ctrl+b"):
            self._await_g = False; self._scroll_up(self._visible_height() * count)
        elif key in ("pagedown", "ctrl+f"):
            self._await_g = False; self._scroll_down(self._visible_height() * count)
        elif key == "ctrl+u":
            self._await_g = False; self._scroll_up(self._visible_height() // 2)
        elif key == "ctrl+d":
            self._await_g = False; self._scroll_down(self._visible_height() // 2)
        elif char == "G" or key in ("f12", "end"):
            # G / F12 / End → go to end (most recent output)
            self._await_g = False; self._scroll_offset = 0; self.refresh()
        elif key in ("f11", "home"):
            # F11 / Home → go to beginning (same as gg)
            self._await_g = False
            self._scroll_offset = len(self._scrollback); self.refresh()
        elif char == "g":
            if self._await_g:
                # gg → go to beginning
                self._await_g = False
                self._scroll_offset = len(self._scrollback); self.refresh()
            else:
                self._await_g = True
        elif key == "apostrophe":
            # '' prefix handled at app level; '.' → jump to last line (bottom)
            self._await_g = False
            self._dot_pending = True
        elif self._dot_pending:
            self._dot_pending = False
            if char == ".":
                self._scroll_offset = 0; self.refresh()
        elif key == "slash":
            self._await_g = False
            self._search_active  = True
            self._search_forward = True
            self._search_buf     = ""
            self.post_message(ScrollSearchStart(True))
        elif key == "question_mark":
            self._await_g = False
            self._search_active  = True
            self._search_forward = False
            self._search_buf     = ""
            self.post_message(ScrollSearchStart(False))
        elif char == "n":
            self._await_g = False; self._do_search(self._search_pattern, self._search_forward)
        elif char == "N":
            self._await_g = False; self._do_search(self._search_pattern, not self._search_forward)
        else:
            self._await_g = False

    def _handle_search_key(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.post_message(ScrollSearchCancel())
        elif key in ("enter", "return"):
            self._search_active   = False
            self._search_pattern  = self._search_buf
            if self._search_pattern:
                self._do_search(self._search_pattern, self._search_forward)
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
        super().__init__(); self.active = active

class ScrollSearchStart(Message):
    def __init__(self, forward: bool) -> None:
        super().__init__(); self.forward = forward

class ScrollSearchUpdate(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__(); self.pattern = pattern

class ScrollSearchCommit(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__(); self.pattern = pattern

class ScrollSearchCancel(Message):
    pass
