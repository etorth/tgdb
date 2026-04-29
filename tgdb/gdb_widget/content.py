"""
Internal terminal-emulation widget for the GDB pane.

This module holds ``_GDBContent``, the pyte-backed implementation behind the
public ``GDBWidget`` wrapper. Splitting it out keeps ``pane.py`` focused on the
public interface while the content widget owns VT100 emulation, resize logic,
scrollback, scroll mode, and key forwarding.
"""

from collections import deque
from collections.abc import Callable

import pyte
from textual.widget import Widget
from textual import events
from rich.text import Text

from ..highlight_groups import HighlightGroups
from .screen import _GDBScreen, _row_to_text
from .scroll import ScrollMixin


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
        self._screen: _GDBScreen | None = None
        self._stream: pyte.ByteStream | None = None
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
            self._pyte_rows = rows
            self._pyte_cols = cols
            self._screen = _GDBScreen(cols, rows, self._push_to_scrollback)
            self._screen.use_color = self.debugwincolor
            self._stream = pyte.ByteStream(self._screen)
        else:
            if rows < self._pyte_rows:
                self._shrink_pyte(rows)
            elif rows > self._pyte_rows:
                self._grow_pyte(rows)
            self._pyte_rows = rows
            self._pyte_cols = cols
            # Let pyte handle column changes and dirty/margin bookkeeping.
            # For shrink: screen.lines was pre-set in _shrink_pyte so pyte
            # sees lines == self.lines and skips its own delete_lines.
            # For grow:   pyte just extends self.lines and updates columns.
            self._screen.resize(rows, cols)


    def _shrink_pyte(self, new_rows: int) -> None:
        """Shrink the pyte screen from self._pyte_rows to new_rows.

        pyte.Screen.resize() would call delete_lines(old - new) from row 0,
        always dropping content from the top regardless of cursor position.
        Instead we push only the rows that would scroll off and shift the
        buffer up manually so the cursor stays on screen.
        """
        cy = self._screen.cursor.y
        buf = self._screen.buffer
        use_color = self._screen.use_color

        top_scroll = max(0, cy + 1 - new_rows)

        # Push displaced rows to both scrollback deques.
        # Save the StaticDefaultDict reference directly (no O(cols) copy);
        # after buf.clear() below pyte no longer holds these refs.
        for r in range(top_scroll):
            row = buf.get(r)
            self._push_to_scrollback(
                _row_to_text(row, self._pyte_cols, use_color=use_color),
                row,
            )

        # Shift the buffer up by top_scroll rows in-place.
        new_entries: dict = {}
        for old_r in list(buf.keys()):
            new_r = old_r - top_scroll
            if 0 <= new_r < new_rows:
                new_entries[new_r] = buf[old_r]
        buf.clear()
        buf.update(new_entries)

        self._screen.cursor.y = max(0, cy - top_scroll)
        # Pre-set line count so pyte's resize() skips delete_lines.
        self._screen.lines = new_rows
        self._screen.dirty.update(range(new_rows))


    def _grow_pyte(self, new_rows: int) -> None:
        """Grow the pyte screen from self._pyte_rows to new_rows.

        Pull rows back from scrollback to fill the newly visible space at
        the top — mirrors cgdb/libvterm grow behaviour.
        """
        n_restore = min(new_rows - self._pyte_rows, len(self._scrollback_raw))
        if not n_restore:
            return

        buf = self._screen.buffer

        # Pop from the RIGHT (most recently pushed = topmost row just before
        # the last shrink).  Reversed so the oldest restored row goes to row 0.
        to_restore: list[dict | None] = []
        for _ in range(n_restore):
            self._scrollback.pop()  # keep display deque in sync
            to_restore.append(self._scrollback_raw.pop())
        ordered = list(reversed(to_restore))

        # Shift existing content down by n_restore.
        new_entries: dict = {}
        for old_r in list(buf.keys()):
            new_entries[old_r + n_restore] = buf[old_r]
        buf.clear()
        buf.update(new_entries)

        # Place restored rows at the top.
        for i, raw in enumerate(ordered):
            if raw is not None:
                buf[i] = raw

        cy = self._screen.cursor.y
        self._screen.cursor.y = min(new_rows - 1, cy + n_restore)
        self._screen.dirty.update(range(new_rows))

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
        row = _row_to_text(
            self._screen.buffer[cy], self._pyte_cols, use_color=False
        ).plain
        before_cursor = row[: self._screen.cursor.x]
        return before_cursor.endswith("(gdb) ")


    def _maybe_escape_burst_key(self, key: str, char: str) -> tuple[str, str] | None:
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
                cursor_col = (
                    cx
                    if (r == cy and not self._scroll_mode and self.gdb_focused)
                    else -1
                )
                lines.append(
                    _row_to_text(
                        buf.get(r),
                        self._pyte_cols,
                        cursor_col,
                        use_color=self.debugwincolor,
                    )
                )
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
            if r == cy and self.gdb_focused:
                cursor_col = cx
            else:
                cursor_col = -1
            # Use buf.get(r) — NOT buf[r] — to avoid creating phantom empty
            # entries in pyte's defaultdict buffer.  Phantom entries confuse
            # pyte's delete_lines logic on the next resize: it sees them as
            # "content" and displaces real rows, clearing the screen.
            result.append_text(
                _row_to_text(
                    buf.get(r),
                    self._pyte_cols,
                    cursor_col,
                    use_color=self.debugwincolor,
                )
            )
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

