"""Search helpers for the internal source-pane content widget."""

from __future__ import annotations

import re
from typing import Optional

from ..source_data import _LOGO_LINES
from ..source_messages import SearchCancel, SearchCommit, SearchStart, SearchUpdate


class SourceSearchMixin:
    """Mixin providing search state and regex navigation for ``_SourceContent``."""

    def search(self, pattern: str, forward: bool = True, start: Optional[int] = None) -> bool:
        if self.source_file:
            lines = self.source_file.lines
        else:
            lines = _LOGO_LINES
        if not lines or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            return False

        start_index = self.sel_line - 1
        if start is not None:
            start_index = start - 1
        line_count = len(lines)
        if forward:
            if self.wrapscan:
                order = list(range(start_index + 1, line_count))
                order.extend(range(0, start_index + 1))
            else:
                order = list(range(start_index + 1, line_count))
        else:
            if self.wrapscan:
                order = list(range(start_index - 1, -1, -1))
                order.extend(range(line_count - 1, start_index - 1, -1))
            else:
                order = list(range(start_index - 1, -1, -1))

        for index in order:
            if regex.search(lines[index]):
                self._last_jump_line = self.sel_line
                self.move_to(index + 1)
                return True
        return False


    def search_next(self) -> bool:
        return self.search(self._search_pattern, self._search_forward)


    def search_prev(self) -> bool:
        return self.search(self._search_pattern, not self._search_forward)


    def run_pending_search(self) -> bool:
        if not self._pending_search:
            return False
        pattern, forward = self._pending_search
        self._pending_search = None
        self._search_pattern = pattern
        self._search_forward = forward
        return self.search(pattern, forward)


    def _start_search(self, forward: bool) -> None:
        self._search_active = True
        self._search_forward = forward
        self._search_buf = ""
        self.post_message(SearchStart(forward=forward))


    def _handle_search_input(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.post_message(SearchCancel())
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            if self._search_pattern:
                initial_source_pending = getattr(self.app, "_initial_source_pending", False)
                if self.source_file is None and initial_source_pending:
                    self._pending_search = (self._search_pattern, self._search_forward)
                else:
                    self.search(self._search_pattern, self._search_forward)
            self.post_message(SearchCommit(self._search_pattern))
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.post_message(SearchUpdate(self._search_buf))
        elif char and char.isprintable():
            self._search_buf += char
            self.post_message(SearchUpdate(self._search_buf))
