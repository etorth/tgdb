"""Search helpers for the file dialog widget."""

from __future__ import annotations

import re


class FileDialogSearchMixin:
    """Mixin providing cgdb-style search behavior for ``FileDialog``."""

    def _search(self, pattern: str, forward: bool, origin: int | None = None) -> bool:
        file_count = len(self._files)
        if file_count == 0 or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            return False

        start = self._sel
        if origin is not None:
            start = origin
        if forward:
            wrap_part = []
            if self.wrapscan:
                wrap_part = list(range(0, start + 1))
            order = list(range(start + 1, file_count))
            order.extend(wrap_part)
        else:
            wrap_part = []
            if self.wrapscan:
                wrap_part = list(range(file_count - 1, start - 1, -1))
            order = list(range(start - 1, -1, -1))
            order.extend(wrap_part)

        for index in order:
            if regex.search(self._files[index]):
                self._sel = index
                self.refresh()
                return True
        return False


    def _handle_search_key(self, key: str, char: str) -> None:
        if key == "escape":
            self._sel = self._search_origin
            self._search_active = False
            self.refresh()
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            self._search(
                self._search_pattern,
                self._search_forward,
                origin=self._search_origin,
            )
            self._search_origin = self._sel
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self._search(
                self._search_buf,
                self._search_forward,
                origin=self._search_origin,
            )
            self.refresh()
        elif char and char.isprintable():
            self._search_buf += char
            self._search(
                self._search_buf,
                self._search_forward,
                origin=self._search_origin,
            )
            self.refresh()
