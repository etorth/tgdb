"""Key-handling helpers for the file dialog widget."""

from textual import events

from .messages import FileDialogClosed, FileSelected


class FileDialogKeyMixin:
    """Mixin providing cgdb-style key handling for ``FileDialog``."""

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if self._query_pending:
            if key in ("q", "escape"):
                self.close()
                self.post_message(FileDialogClosed())
            event.stop()
            return

        if self._search_active:
            self._handle_search_key(key, char)
            event.stop()
            return

        if self._await_g:
            self._await_g = False
            if char == "g":
                self._sel = 0
                self._num_buf = ""
                self.refresh()
                event.stop()
                return
            self._num_buf = ""

        if char.isdigit() and (char != "0" or self._num_buf):
            self._num_buf += char
            event.stop()
            return
        count = int(self._num_buf) if self._num_buf else 1
        had_count = bool(self._num_buf)
        self._num_buf = ""

        if key in ("q", "escape"):
            self.close()
            self.post_message(FileDialogClosed())
        elif key in ("j", "down"):
            self._move(count)
        elif key in ("k", "up"):
            self._move(-count)
        elif key in ("h", "left"):
            self._scroll_h(-count)
        elif key in ("l", "right"):
            self._scroll_h(count)
        elif key in ("ctrl+f", "pagedown"):
            self._move(self._list_height() * count)
        elif key in ("ctrl+b", "pageup"):
            self._move(-self._list_height() * count)
        elif key == "ctrl+d":
            self._move(self._list_height() // 2)
        elif key == "ctrl+u":
            self._move(-self._list_height() // 2)
        elif char == "G":
            if had_count:
                target_index = count - 1
            else:
                target_index = len(self._files) - 1
            self._sel = self._clamp(target_index)
            self.refresh()
        elif char == "g":
            self._await_g = True
        elif key == "slash":
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
            self._search_origin = self._sel
            self.refresh()
        elif key == "question_mark":
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
            self._search_origin = self._sel
            self.refresh()
        elif char == "n":
            self._search(self._search_pattern, self._search_forward)
        elif char == "N":
            self._search(self._search_pattern, not self._search_forward)
        elif key in ("enter", "return"):
            if 0 <= self._sel < len(self._files):
                self.close()
                self.post_message(FileSelected(self._files[self._sel]))
        event.stop()
