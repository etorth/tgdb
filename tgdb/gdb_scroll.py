"""
Scroll-mode mixin for GDBWidget.

Extracts all scroll-mode rendering, navigation, search, and key handling
into a mixin class so gdb_widget.py stays focused on core terminal
emulation and live rendering.
"""
from __future__ import annotations

import re

from textual.message import Message
from rich.text import Text


# ---------------------------------------------------------------------------
# Messages (posted by scroll-mode methods)
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


# ---------------------------------------------------------------------------
# ScrollMixin — mixed into GDBWidget
# ---------------------------------------------------------------------------

class ScrollMixin:
    """Scroll-mode rendering, navigation, search, and key handling."""

    # ------------------------------------------------------------------
    # Scroll helpers
    # ------------------------------------------------------------------

    def enter_scroll_mode(self) -> None:
        self._scroll_mode = True
        self._scroll_offset = 0
        self.post_message(ScrollModeChange(True))
        self.refresh()

    def exit_scroll_mode(self) -> None:
        self._scroll_mode = False
        self._scroll_offset = 0
        self._h_offset = 0
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

    # ------------------------------------------------------------------
    # Scroll-mode rendering
    # ------------------------------------------------------------------

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
    # Search
    # ------------------------------------------------------------------

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
        h = self._visible_height()
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
    # Key handling — scroll mode
    # ------------------------------------------------------------------

    def _handle_scroll_key(self, key: str, char: str) -> None:
        if char.isdigit() and char != "0":
            self._num_buf += char
            return
        count = int(self._num_buf) if self._num_buf else 1
        self._num_buf = ""

        if key == "escape":
            self.on_switch_to_tgdb()
            self.exit_scroll_mode()
        elif key in ("q", "i", "enter", "return"):
            self.exit_scroll_mode()
        elif key in ("j", "down", "ctrl+n"):
            self._await_g = False
            self._scroll_down(count)
        elif key in ("k", "up", "ctrl+p"):
            self._await_g = False
            self._scroll_up(count)
        elif key in ("h", "left"):
            self._await_g = False
            self._scroll_left()
        elif key in ("l", "right"):
            self._await_g = False
            self._scroll_right()
        elif char == "0":
            self._await_g = False
            self._beginning_of_row()
        elif char == "$":
            self._await_g = False
            self._end_of_row()
        elif key in ("pageup", "ctrl+b"):
            self._await_g = False
            self._scroll_up(self._visible_height() * count)
        elif key in ("pagedown", "ctrl+f"):
            self._await_g = False
            self._scroll_down(self._visible_height() * count)
        elif key == "ctrl+u":
            self._await_g = False
            self._scroll_up(self._visible_height() // 2)
        elif key == "ctrl+d":
            self._await_g = False
            self._scroll_down(self._visible_height() // 2)
        elif char == "G" or key in ("f12", "end"):
            # G / F12 / End → go to end (most recent output)
            self._await_g = False
            self._scroll_offset = 0
            self.refresh()
        elif key in ("f11", "home"):
            # F11 / Home → go to beginning (same as gg)
            self._await_g = False
            self._scroll_offset = len(self._scrollback)
            self.refresh()
        elif char == "g":
            if self._await_g:
                # gg → go to beginning
                self._await_g = False
                self._scroll_offset = len(self._scrollback)
                self.refresh()
            else:
                self._await_g = True
        elif key == "apostrophe":
            # '' prefix handled at app level; '.' → jump to last line (bottom)
            self._await_g = False
            self._dot_pending = True
        elif self._dot_pending:
            self._dot_pending = False
            if char == ".":
                self._scroll_offset = 0
                self.refresh()
        elif key == "slash":
            self._await_g = False
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
            self.post_message(ScrollSearchStart(True))
        elif key == "question_mark":
            self._await_g = False
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
            self.post_message(ScrollSearchStart(False))
        elif char == "n":
            self._await_g = False
            self._do_search(self._search_pattern, self._search_forward)
        elif char == "N":
            self._await_g = False
            self._do_search(self._search_pattern, not self._search_forward)
        else:
            self._await_g = False

    # ------------------------------------------------------------------
    # Key handling — search input
    # ------------------------------------------------------------------

    def _handle_search_key(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.post_message(ScrollSearchCancel())
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            if self._search_pattern:
                self._do_search(self._search_pattern, self._search_forward)
            self.post_message(ScrollSearchCommit(self._search_pattern))
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.post_message(ScrollSearchUpdate(self._search_buf))
        elif char and char.isprintable():
            self._search_buf += char
            self.post_message(ScrollSearchUpdate(self._search_buf))
