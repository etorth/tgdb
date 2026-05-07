"""
Scroll-mode mixin for GDBWidget.

Extracts all scroll-mode rendering, navigation, search, and key handling
into a mixin class so the GDB-widget package can keep its pane implementation
focused on core terminal emulation and live rendering.
"""

import re

from textual.message import Message
from rich.cells import cell_len
from rich.text import Text


def _drop_left_cells(line: Text, cells: int) -> Text:
    """Return *line* with the leading *cells* display cells removed.

    Counts cell width per character (wide chars count as 2 cells) so
    CJK / emoji content stays column-aligned after horizontal scrolling.

    When the cut point falls inside a wide character, that character
    is dropped and the lost cells are filled with single-cell ``?``
    placeholders.  This keeps the right-hand content in the same
    physical column it would occupy with a perfect cut: ``"你好"``
    cut by 1 cell renders as ``"?好"`` (the leading half of ``你`` is
    gone but the column ``好`` would be in is preserved).  Without
    the placeholder, ``好`` would shift one column left every time
    the user scrolled by an odd number of cells through wide content.
    """
    if cells <= 0:
        return line
    plain = line.plain
    used = 0
    char_idx = 0
    pad = 0
    for ch in plain:
        w = cell_len(ch)
        if used + w > cells:
            # ch straddles the cut.  Skip it and emit ``pad``
            # placeholder cells (always 1 in practice — the only
            # straddle for a 2-cell-wide char is a 1-cell offset)
            # so the right-side content stays in column.
            pad = used + w - cells
            char_idx += 1
            break
        used += w
        char_idx += 1
        if used == cells:
            break

    result = line[char_idx:]
    if pad > 0:
        result = Text("?" * pad) + result
    return result


def _max_cell_width(lines: list[Text], start: int, count: int) -> int:
    """Greatest cell width among ``lines[start : start + count]``."""
    end = min(start + count, len(lines))
    if start >= end:
        return 0
    return max(cell_len(lines[i].plain) for i in range(start, end))


def _truncate_to_cells(line: Text, cells: int) -> Text:
    """Return *line* truncated from the right to fit in *cells* display cells.

    Mirrors ``_drop_left_cells`` semantics for the right side: when a
    wide character straddles the cut point, it is dropped and the
    leftover cells are filled with single-cell ``?`` placeholders so
    the overlay column ends up exactly where it should.  ``"你好world"``
    truncated to 3 cells renders as ``"你?"`` (the first wide char fits
    and the half-cut second wide char becomes a single ``?``).

    When the line is naturally shorter than *cells* the result keeps
    its original length — no padding — matching the upstream behaviour
    for the case where there's nothing to truncate.
    """
    if cells <= 0:
        return Text()
    plain = line.plain
    used = 0
    char_idx = 0
    pad = 0
    for ch in plain:
        w = cell_len(ch)
        if used + w > cells:
            # ch straddles the cut.  Drop it and emit ``pad`` ?
            # placeholders so the right-side column is preserved.
            pad = cells - used
            break
        used += w
        char_idx += 1
        if used == cells:
            break
    truncated = line[:char_idx]
    if pad > 0:
        truncated = truncated + Text("?" * pad)
    return truncated


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
        # Clamp ``_h_offset`` so it cannot grow beyond the longest visible
        # line.  Without this the offset increments unbounded — eventually
        # the slice in ``_render_scroll`` returns empty Text and the user
        # sees blank rows with no upper bound on how far they've scrolled.
        lines = self._all_lines()
        total = len(lines)
        h = self._visible_height()
        start = max(0, total - h - self._scroll_offset)
        max_cells = _max_cell_width(lines, start, h)
        # Keep at least one cell of content visible at the right edge.
        if self._h_offset + 1 < max_cells:
            self._h_offset += 1
            self.refresh()


    def _beginning_of_row(self) -> None:
        """Jump to start of row (cgdb scr_beginning_of_row, key '0')."""
        self._h_offset = 0
        self.refresh()


    def _end_of_row(self) -> None:
        """Jump to end of row (cgdb scr_end_of_row, key '$')."""
        # Measure longest visible line in display cells, not characters,
        # so wide chars (CJK / emoji) participate correctly.
        lines = self._all_lines()
        total = len(lines)
        h = self._visible_height()
        start = max(0, total - h - self._scroll_offset)
        w = max(80, self.size.width or 80)
        max_w = _max_cell_width(lines, start, h) or w
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
                        if self.ignorecase:
                            flags = re.IGNORECASE
                        else:
                            flags = 0
                        rx = re.compile(self._search_pattern, flags)
                        for m in rx.finditer(line.plain):
                            line.stylize(self.hl.style("Search"), m.start(), m.end())
                    except re.error:
                        pass
                # Apply horizontal offset (cgdb scroll_cursor_col).  Use
                # cell-aware slicing so wide chars (CJK / emoji) stay
                # aligned: ``_h_offset`` is measured in display cells, not
                # characters.
                if self._h_offset > 0:
                    if self._h_offset < cell_len(line.plain):
                        line = _drop_left_cells(line, self._h_offset)
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
            # ``stat`` is ASCII so ``len(stat) == cell_len(stat)``.  The
            # truncation of the line, however, must be cell-aware:
            # ``first[:left_width]`` slices by character, which produces a
            # wrong-width result when ``first`` contains wide chars (each
            # wide char counts as 1 char but 2 cells).  ``_truncate_to_cells``
            # measures by cells and pads with spaces so the overlay stays
            # flush-right at column ``w``.
            left_width = max(0, w - len(stat))
            rendered_lines[0] = Text.assemble(
                _truncate_to_cells(first, left_width),
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
        if self.ignorecase:
            flags = re.IGNORECASE
        else:
            flags = 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        h = self._visible_height()
        cur = max(0, total - h - self._scroll_offset)
        if forward:
            if self.wrapscan:
                wrap_part = list(range(0, cur + 1))
            else:
                wrap_part = []
            order = list(range(cur + 1, total)) + wrap_part
        else:
            if self.wrapscan:
                wrap_part = list(range(total - 1, cur - 1, -1))
            else:
                wrap_part = []
            order = list(range(cur - 1, -1, -1)) + wrap_part
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

        # 'g' double-press (gg) — must check before resetting _await_g
        if char == "g":
            if self._await_g:
                self._await_g = False
                self._scroll_offset = len(self._scrollback)
                self.refresh()
            else:
                self._await_g = True
            return

        # All other keys reset the pending 'g' state
        self._await_g = False

        if key == "escape":
            self.on_switch_to_tgdb()
            self.exit_scroll_mode()
        elif key in ("q", "i", "enter", "return"):
            self.exit_scroll_mode()
        elif key in ("j", "down", "ctrl+n"):
            self._scroll_down(count)
        elif key in ("k", "up", "ctrl+p"):
            self._scroll_up(count)
        elif key in ("h", "left"):
            self._scroll_left()
        elif key in ("l", "right"):
            self._scroll_right()
        elif char == "0":
            self._beginning_of_row()
        elif char == "$":
            self._end_of_row()
        elif key in ("pageup", "ctrl+b"):
            self._scroll_up(self._visible_height() * count)
        elif key in ("pagedown", "ctrl+f"):
            self._scroll_down(self._visible_height() * count)
        elif key == "ctrl+u":
            self._scroll_up(self._visible_height() // 2)
        elif key == "ctrl+d":
            self._scroll_down(self._visible_height() // 2)
        elif char == "G" or key in ("f12", "end"):
            self._scroll_offset = 0
            self.refresh()
        elif key in ("f11", "home"):
            self._scroll_offset = len(self._scrollback)
            self.refresh()
        elif key == "apostrophe":
            # '' prefix handled at app level; '.' → jump to bottom
            self._dot_pending = True
        elif self._dot_pending:
            self._dot_pending = False
            if char == ".":
                self._scroll_offset = 0
                self.refresh()
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
            self._do_search(self._search_pattern, self._search_forward)
        elif char == "N":
            self._do_search(self._search_pattern, not self._search_forward)



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
