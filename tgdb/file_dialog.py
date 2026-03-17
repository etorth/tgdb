"""
File dialog widget — mirrors cgdb's filedlg.cpp.

Full-screen list of source files with vi-like navigation and regex search.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text

from .highlight_groups import HighlightGroups


class FileDialog(Widget):
    """
    Full-screen file picker.

    Set .files to a list of file paths.
    Emits FileSelected(path) when user presses Enter.
    Emits FileDialogClosed() when user presses q.
    """

    DEFAULT_CSS = """
    FileDialog {
        layer: dialog;
        background: $surface;
        width: 1fr;
        height: 1fr;
        display: none;
    }
    FileDialog.visible {
        display: block;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._files: list[str] = []
        self._displayed: list[str] = []   # filtered view
        self._sel: int = 0                # selected index
        self._scroll_top: int = 0
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self._search_pattern: str = ""
        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self._num_buf: str = ""
        self.can_focus = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def files(self) -> list[str]:
        return self._files

    @files.setter
    def files(self, value: list[str]) -> None:
        self._files = sorted(value)
        self._displayed = list(self._files)
        self._sel = 0
        self._scroll_top = 0
        self.refresh()

    def open(self) -> None:
        self.add_class("visible")
        self._displayed = list(self._files)
        self._sel = 0
        self._scroll_top = 0
        self.focus()
        self.refresh()

    def close(self) -> None:
        self.remove_class("visible")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _visible_height(self) -> int:
        return max(1, self.size.height - 2)   # leave room for header/search bar

    def _ensure_visible(self) -> None:
        h = self._visible_height()
        if self._sel < self._scroll_top:
            self._scroll_top = self._sel
        elif self._sel >= self._scroll_top + h:
            self._scroll_top = self._sel - h + 1

    def _move(self, delta: int) -> None:
        n = len(self._displayed)
        if n == 0:
            return
        self._sel = max(0, min(n - 1, self._sel + delta))
        self._ensure_visible()
        self.refresh()

    def _page_up(self) -> None:
        self._move(-self._visible_height())

    def _page_down(self) -> None:
        self._move(self._visible_height())

    def _goto_top(self) -> None:
        self._sel = 0
        self._scroll_top = 0
        self.refresh()

    def _goto_bottom(self, line: Optional[int] = None) -> None:
        n = len(self._displayed)
        self._sel = max(0, (line - 1) if line is not None else n - 1)
        self._ensure_visible()
        self.refresh()

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------

    def _search(self, pattern: str, forward: bool) -> bool:
        n = len(self._displayed)
        if n == 0 or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        start = self._sel
        if forward:
            indices = list(range(start + 1, n)) + (list(range(0, start + 1)) if self.wrapscan else [])
        else:
            indices = list(range(start - 1, -1, -1)) + (list(range(n - 1, start - 1, -1)) if self.wrapscan else [])
        for idx in indices:
            if rx.search(os.path.basename(self._displayed[idx])):
                self._sel = idx
                self._ensure_visible()
                self.refresh()
                return True
        return False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w = self.size.width or 80
        h = self._visible_height()
        result = Text()

        # Header
        header_style = self.hl.style("StatusLine")
        n = len(self._displayed)
        header = f" Source Files ({n} files) ".ljust(w)
        result.append(header[:w], style=header_style)
        result.append("\n")

        for y in range(h):
            idx = self._scroll_top + y
            if idx >= n:
                result.append(" " * w + "\n")
                continue
            path = self._displayed[idx]
            basename = os.path.basename(path)
            is_sel = (idx == self._sel)
            if is_sel:
                style = self.hl.style("SelectedLineHighlight")
            else:
                style = ""
            line = f" {basename} "
            if len(line) < w:
                line = line + " " * (w - len(line))
            result.append(line[:w], style=style)
            result.append("\n")

        # Search bar / instruction
        if self._search_active:
            pfx = "/" if self._search_forward else "?"
            bar_style = self.hl.style("StatusLine")
            result.append(f"{pfx}{self._search_buf}".ljust(w)[:w], style=bar_style)
        else:
            result.append(
                f" j/k=move  /=search  Enter=open  q=quit".ljust(w)[:w],
                style=self.hl.style("StatusLine")
            )
        return result

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if self._search_active:
            self._handle_search_input(key, char)
            event.stop()
            return

        if char.isdigit() and char not in ("0",):
            self._num_buf += char
            event.stop()
            return
        count = int(self._num_buf) if self._num_buf else 1
        self._num_buf = ""

        if key in ("q", "escape"):
            self.post_message(FileDialogClosed())
        elif key in ("j", "down"):
            self._move(count)
        elif key in ("k", "up"):
            self._move(-count)
        elif key in ("h", "left"):
            pass
        elif key in ("l", "right"):
            pass
        elif key in ("ctrl+f", "pagedown"):
            self._page_down()
        elif key in ("ctrl+b", "pageup"):
            self._page_up()
        elif key == "ctrl+d":
            self._move(self._visible_height() // 2)
        elif key == "ctrl+u":
            self._move(-self._visible_height() // 2)
        elif key == "G":
            self._goto_bottom()
        elif key == "g":
            self._goto_top()
        elif key == "slash":
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
        elif key == "question_mark":
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
        elif key == "n":
            self._search(self._search_pattern, self._search_forward)
        elif key == "N":
            self._search(self._search_pattern, not self._search_forward)
        elif key in ("enter", "return"):
            if 0 <= self._sel < len(self._displayed):
                self.post_message(FileSelected(self._displayed[self._sel]))
        event.stop()

    def _handle_search_input(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.refresh()
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            self._search(self._search_pattern, self._search_forward)
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.refresh()
        elif char and char.isprintable():
            self._search_buf += char
            self.refresh()


# Messages

class FileSelected(Message):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

class FileDialogClosed(Message):
    pass
