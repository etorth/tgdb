"""Navigation helpers for the internal source-pane content widget."""

from __future__ import annotations

from typing import Optional

from ..source_data import _LOGO_LINES
from ..source_messages import JumpGlobalMark


class SourceNavigationMixin:
    """Mixin providing cursor, scrolling, and mark navigation for ``_SourceContent``."""

    def _line_count(self) -> int:
        if self.source_file:
            return len(self.source_file.lines)
        return len(_LOGO_LINES)


    def _visible_height(self) -> int:
        return max(1, self.size.height)


    def _ensure_visible(self, line: int) -> None:
        """Mirror cgdb: keep the selected line centered when possible."""
        height = self._visible_height()
        line_count = self._line_count()
        index = line - 1
        if line_count <= 0 or line_count < height:
            self._scroll_top = 0
        else:
            self._scroll_top = max(0, min(index - height // 2, line_count - height))


    def move_to(self, line: int) -> None:
        line_count = self._line_count()
        if line_count:
            self.sel_line = max(1, min(line, line_count))
        else:
            self.sel_line = max(1, min(line, 1))
        self._ensure_visible(self.sel_line)
        self.refresh()


    def scroll_up(self, n: int = 1) -> None:
        self.move_to(self.sel_line - n)


    def scroll_down(self, n: int = 1) -> None:
        self.move_to(self.sel_line + n)


    def scroll_col(self, delta: int) -> None:
        """Horizontal scroll — cgdb sel_col."""
        self._col_offset = max(0, self._col_offset + delta)
        self.refresh()


    def scroll_col_to(self, col: int) -> None:
        """Set horizontal scroll to an absolute display-column position."""
        if col >= 999999:
            if self.source_file and 1 <= self.sel_line <= len(self.source_file.lines):
                from rich.cells import cell_len as _cell_len

                line_text = self.source_file.lines[self.sel_line - 1]
                line_cells = _cell_len(line_text)
                visible_width = max(1, (self.size.width or 80) - 6)
                col = max(0, line_cells - visible_width)
            else:
                col = 0
        self._col_offset = max(0, col)
        self.refresh()


    def page_up(self) -> None:
        height = self._visible_height()
        self.sel_line = max(1, self.sel_line - height)
        self._ensure_visible(self.sel_line)
        self.refresh()


    def page_down(self) -> None:
        height = self._visible_height()
        line_count = self._line_count()
        if line_count:
            self.sel_line = min(line_count, self.sel_line + height)
        else:
            self.sel_line = min(1, self.sel_line + height)
        self._ensure_visible(self.sel_line)
        self.refresh()


    def half_page_up(self) -> None:
        self.scroll_up(self._visible_height() // 2)


    def half_page_down(self) -> None:
        self.scroll_down(self._visible_height() // 2)


    def goto_top(self) -> None:
        self.move_to(1)


    def goto_bottom(self, line: Optional[int] = None) -> None:
        if line is not None:
            self.move_to(line)
        else:
            self.move_to(self._line_count())


    def goto_executing(self) -> None:
        if self.exe_line > 0:
            self._last_jump_line = self.sel_line
            self.move_to(self.exe_line)


    def goto_last_jump(self) -> None:
        target_line = self._last_jump_line
        self._last_jump_line = self.sel_line
        self.move_to(target_line)


    def show_logo(self) -> None:
        self._show_logo = True
        self.refresh()


    def goto_screen_top(self) -> None:
        self.move_to(self._scroll_top + 1)


    def goto_screen_middle(self) -> None:
        self.move_to(self._scroll_top + self._visible_height() // 2 + 1)


    def goto_screen_bottom(self) -> None:
        self.move_to(self._scroll_top + self._visible_height())


    def set_mark(self, ch: str) -> None:
        source_file = self.source_file
        if not source_file:
            return
        if ch.islower():
            source_file.marks_local[ch] = self.sel_line
        else:
            self._global_marks[ch] = (source_file.path, self.sel_line)


    def jump_to_mark(self, ch: str) -> bool:
        source_file = self.source_file
        if ch.islower():
            line = None
            if source_file:
                line = source_file.marks_local.get(ch)
            if line is not None:
                self._last_jump_line = self.sel_line
                self.move_to(line)
                return True
        else:
            mark = self._global_marks.get(ch)
            if mark:
                path, line = mark
                if source_file and source_file.path == path:
                    self._last_jump_line = self.sel_line
                    self.move_to(line)
                    return True
                self.post_message(JumpGlobalMark(path, line))
                return True
        return False
