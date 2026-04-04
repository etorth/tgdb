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

from collections import deque
from typing import Callable, Optional

import pyte
from textual.widget import Widget
from textual import events
from rich.text import Text

from .highlight_groups import HighlightGroups


# ---------------------------------------------------------------------------
# pyte colour → Rich colour conversion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GDB widget
# ---------------------------------------------------------------------------

from .gdb_screen import _GDBScreen, _row_to_text  # noqa: F401
from .gdb_scroll import ScrollMixin  # noqa: F401
from .gdb_scroll import (  # noqa: F401 — re-exported
    ScrollModeChange,
    ScrollSearchStart,
    ScrollSearchUpdate,
    ScrollSearchCommit,
    ScrollSearchCancel,
)


from .pane_base import PaneBase


class _GDBContent(ScrollMixin, Widget):
    """
    Internal VT100 terminal widget (pyte) connected to GDB's PTY.
    Hosted inside GDBWidget (PaneBase) which provides the title bar.
    """

    DEFAULT_CSS = """
    _GDBContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, max_scrollback: int = 10000, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.max_scrollback = max_scrollback
        self.can_focus = False
        self.gdb_focused: bool = True
        self._scrollback: deque[Text] = deque(maxlen=max_scrollback)
        self._scrollback_raw: deque = deque(maxlen=max_scrollback)
        self.debugwincolor: bool = True
        self._screen: Optional[_GDBScreen] = None
        self._stream: Optional[pyte.ByteStream] = None
        self._pyte_rows: int = 24
        self._pyte_cols: int = 80
        self._init_pyte(24, 80)
        self._scroll_mode: bool = False
        self._scroll_offset: int = 0
        self._h_offset: int = 0
        self._search_pattern: str = ""
        self._search_forward: bool = True
        self._search_active: bool = False
        self._search_buf: str = ""
        self.send_to_gdb: Callable[[bytes], None] = lambda b: None
        self.resize_gdb: Callable[[int, int], None] = lambda r, c: None
        self.on_switch_to_tgdb: Callable[[], None] = lambda: None
        self.imap_feed: Callable[[str], "list[str] | None"] = lambda k: None
        self.imap_replay: Callable[["list[str]"], None] = lambda tokens: None
        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self._num_buf: str = ""
        self._await_g: bool = False
        self._dot_pending: bool = False
        self._gdb_resize_notified: tuple[int, int] = (24, 80)

    # ------------------------------------------------------------------
    # Scrollback helpers
    # ------------------------------------------------------------------

    def _push_to_scrollback(self, text: Text, raw=None) -> None:
        """Append one line to both scrollback deques (text for display, raw for restoration)."""
        self._scrollback.append(text)
        self._scrollback_raw.append(raw)  # dict copy of pyte row, or None

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

                # Push displaced rows to both scrollback deques (text + raw ref).
                # We save the StaticDefaultDict reference directly — no O(cols)
                # dict() copy.  After buf.clear() below, pyte no longer holds
                # these references, so _scrollback_raw is the sole owner and
                # pyte cannot mutate them.
                for r in range(top_scroll):
                    row = buf.get(r)
                    self._push_to_scrollback(
                        _row_to_text(row, self._pyte_cols, use_color=use_color),
                        row,  # save reference, not a copy
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
                grow = rows - self._pyte_rows
                n_restore = min(grow, len(self._scrollback_raw))

                if n_restore > 0:
                    buf = self._screen.buffer

                    # Pop from the RIGHT (most recently pushed = was topmost row
                    # just before the last shrink).  Collect in push order so
                    # the oldest of the restored set goes to row 0 and the
                    # most-recent goes to row n_restore-1 (just above content).
                    to_restore: list[Optional[dict]] = []
                    for _ in range(n_restore):
                        self._scrollback.pop()  # keep display deque in sync
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
                lines.append(_row_to_text(buf.get(r), self._pyte_cols, cursor_col, use_color=self.debugwincolor))
        return lines

    def render(self) -> Text:
        # cgdb calls if_layout() → scr_move() (resize libvterm) → if_draw()
        # in one synchronous step, so the draw always sees the correct size.
        # Textual fires on_resize AFTER at least one frame has been rendered at
        # the new widget size, causing a one-frame flash of stale content.
        # Mirror cgdb's approach: eagerly resize the pyte buffer here so the
        # very first render at the new size already shows the correct content.
        # on_resize() will still send SIGWINCH to GDB via _gdb_resize_notified.
        h = self._visible_height()
        w = self.size.width
        if self._screen and w > 0 and (h != self._pyte_rows or w != self._pyte_cols):
            self._init_pyte(h, w)
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
            result.append_text(_row_to_text(buf.get(r), self._pyte_cols, cursor_col, use_color=self.debugwincolor))
        # Pad remaining rows if widget is taller than pyte screen
        for r in range(self._pyte_rows, h):
            result.append("\n")
        return result

    # ------------------------------------------------------------------
    # Resize — keep pyte screen in sync with widget size
    # ------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        new_rows = event.size.height
        new_cols = event.size.width
        if new_rows != self._pyte_rows or new_cols != self._pyte_cols:
            self._init_pyte(new_rows, new_cols)
        # Always notify GDB of the new size if it hasn't been told yet.
        # render() may have already called _init_pyte (eager resize to avoid
        # flash), so we track SIGWINCH notifications separately.
        if (new_rows, new_cols) != self._gdb_resize_notified:
            self.resize_gdb(new_rows, new_cols)
            self._gdb_resize_notified = (new_rows, new_cols)
        self.refresh()

    # ------------------------------------------------------------------
    # Scroll helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    # Textual key name → raw bytes to write to GDB's PTY
    _KEY_BYTES: dict[str, bytes] = {
        "enter": b"\r",
        "return": b"\r",
        "backspace": b"\x7f",
        "ctrl+h": b"\x08",
        "tab": b"\t",
        "ctrl+c": b"\x03",
        "ctrl+d": b"\x04",
        "ctrl+a": b"\x01",
        "ctrl+b": b"\x02",
        "ctrl+e": b"\x05",
        "ctrl+f": b"\x06",
        "ctrl+k": b"\x0b",
        "ctrl+n": b"\x0e",
        "ctrl+p": b"\x10",
        "ctrl+r": b"\x12",
        "ctrl+u": b"\x15",
        "ctrl+w": b"\x17",
        "ctrl+l": b"\x0c",
        "escape": b"\x1b",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
        "home": b"\x1b[H",
        "end": b"\x1b[F",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
        "delete": b"\x1b[3~",
        "f1": b"\x1bOP",
        "f2": b"\x1bOQ",
        "f3": b"\x1bOR",
        "f4": b"\x1bOS",
        "f5": b"\x1b[15~",
        "f6": b"\x1b[17~",
        "f7": b"\x1b[18~",
        "f8": b"\x1b[19~",
        "f10": b"\x1b[21~",
        "f11": b"\x1b[23~",
        "f12": b"\x1b[24~",
    }

    def on_key(self, event: events.Key) -> None:
        key = event.key
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
            self.on_switch_to_tgdb()
            if callable(handler):
                handler(*burst_key)
            event.stop()
            return

        # Normal mode — ESC switches to TGDB source pane
        if key == "escape":
            self.on_switch_to_tgdb()
            event.stop()
            return
        # PageUp enters scroll mode
        if key == "pageup":
            self.enter_scroll_mode()
            self._scroll_up(self._visible_height())
            event.stop()
            return

        # Check imap before forwarding to GDB's PTY
        imap_result = self.imap_feed(key)
        if imap_result is None:
            # Buffering — consumed but not yet resolved
            event.stop()
            return
        if imap_result != [key]:
            # An imap fired — replay the expansion
            self.imap_replay(imap_result)
            event.stop()
            return

        # No imap matched — forward key directly to GDB's PTY
        raw = self._KEY_BYTES.get(key)
        if raw:
            self.send_to_gdb(raw)
        elif char and (char.isprintable() or char == "\t"):
            self.send_to_gdb(char.encode())
        event.stop()


# ---------------------------------------------------------------------------
# Public pane wrapper
# ---------------------------------------------------------------------------

# Attributes that must be forwarded to _GDBContent on set.
_GDB_DELEGATE_SET = frozenset(
    {
        "send_to_gdb",
        "resize_gdb",
        "on_switch_to_tgdb",
        "imap_feed",
        "imap_replay",
        "gdb_focused",
        "debugwincolor",
        "ignorecase",
        "wrapscan",
        "max_scrollback",
    }
)


class GDBWidget(PaneBase):
    """GDB console pane: title bar (blank) + _GDBContent terminal widget."""

    def __init__(self, hl: HighlightGroups, max_scrollback: int = 10000, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _GDBContent(hl, max_scrollback)
        self.can_focus = True

    def title(self) -> Optional[str]:
        return None

    def compose(self):
        yield from super().compose()
        yield self._content

    # ------------------------------------------------------------------
    # Key and refresh delegation
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        self._content.on_key(event)

    def refresh(self, *args, **kwargs):
        if self._content.is_mounted:
            self._content.refresh(*args, **kwargs)
        return super().refresh(*args, **kwargs)

    # ------------------------------------------------------------------
    # Attribute delegation to _GDBContent
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value) -> None:
        if name in _GDB_DELEGATE_SET and "_content" in self.__dict__:
            setattr(self._content, name, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name: str):
        content = self.__dict__.get("_content")
        if content is not None:
            return getattr(content, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
