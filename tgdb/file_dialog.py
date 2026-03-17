"""
File dialog widget — mirrors cgdb's filedlg.cpp exactly.

Layout (matches cgdb filedlg_display()):
  Row 0:        "Select a file or press q to cancel."  (centered)
  Rows 1..h-2:  {nr:Nd}->{path}   ← selected line  (bold nr, arrow in SelectedLineArrow style)
                {nr:Nd}│ {path}   ← normal line    (bold │)
                   ~│             ← filler beyond EOF
  Row h-1:      status bar (search prompt or key hints)
  Row h:        [blank, used as ncurses border equivalent]
"""
from __future__ import annotations

import math
import os
import re
from typing import Optional

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text

from .highlight_groups import HighlightGroups


class FileDialog(Widget):
    """Full-screen file picker matching cgdb filedlg.cpp layout."""

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

    _LABEL = "Select a file or press q to cancel."

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._files: list[str] = []
        self._sel: int = 0          # 0-based selected index into _files
        self._sel_col: int = 0      # horizontal scroll (matches filedlg sel_col)
        self._search_active: bool  = False
        self._search_buf: str      = ""
        self._search_forward: bool = True
        self._search_pattern: str  = ""
        self.ignorecase: bool = False
        self.wrapscan:   bool = True
        self._num_buf:   str  = ""
        self._await_g:   bool = False
        self.can_focus = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def files(self) -> list[str]:
        return self._files

    @files.setter
    def files(self, value: list[str]) -> None:
        self._files = list(value)   # already sorted by controller
        self._sel   = 0
        self._sel_col = 0
        self.refresh()

    def open(self) -> None:
        self._sel = 0
        self._sel_col = 0
        self._search_active = False
        self.add_class("visible")
        self.focus()
        self.refresh()

    def close(self) -> None:
        self.remove_class("visible")

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _h(self) -> int:
        """Total visible height."""
        return max(3, self.size.height)

    def _w(self) -> int:
        return max(10, self.size.width)

    def _list_height(self) -> int:
        """Lines available for the file list (total - label row - status row)."""
        return max(1, self._h() - 2)

    def _lwidth(self) -> int:
        """Width of line-number field = floor(log10(count)) + 1, min 1."""
        n = len(self._files)
        if n == 0:
            return 1
        return int(math.log10(n)) + 1

    def _scroll_start(self) -> int:
        """First file index to show (centres selected line, cgdb style)."""
        count  = len(self._files)
        height = self._list_height()
        if count < height:
            return (count - height) // 2   # may be negative → clamp below
        start = self._sel - height // 2
        start = max(0, min(count - height, start))
        return start

    # ------------------------------------------------------------------
    # Navigation (mirrors filedlg_move / filedlg_set_sel_line)
    # ------------------------------------------------------------------

    def _clamp(self, line: int) -> int:
        return max(0, min(len(self._files) - 1, line))

    def _move(self, delta: int) -> None:
        if not self._files:
            return
        self._sel = self._clamp(self._sel + delta)
        self.refresh()

    def _scroll_h(self, delta: int) -> None:
        """Horizontal scroll (sel_col in cgdb)."""
        max_w = max((len(p) for p in self._files), default=0)
        self._sel_col = max(0, min(max_w, self._sel_col + delta))
        self.refresh()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search(self, pattern: str, forward: bool) -> bool:
        n = len(self._files)
        if n == 0 or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        start = self._sel
        order = (
            list(range(start + 1, n)) + (list(range(0, start + 1)) if self.wrapscan else [])
            if forward else
            list(range(start - 1, -1, -1)) + (list(range(n - 1, start - 1, -1)) if self.wrapscan else [])
        )
        for idx in order:
            if rx.search(self._files[idx]):
                self._sel = idx
                self.refresh()
                return True
        return False

    # ------------------------------------------------------------------
    # Rendering — matches filedlg_display() exactly
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w      = self._w()
        h      = self._h()
        lh     = self._list_height()
        lwidth = self._lwidth()
        count  = len(self._files)
        result = Text(no_wrap=True, overflow="crop")

        # ── Row 0: centred label (cgdb print_in_middle) ──
        label  = self._LABEL
        pad    = max(0, (w - len(label)) // 2)
        result.append(" " * pad + label, style=self.hl.style("StatusLine"))
        result.append(" " * max(0, w - pad - len(label)),
                      style=self.hl.style("StatusLine"))
        result.append("\n")

        # ── Rows 1..lh: file list ──
        start = self._scroll_start()

        for i in range(1, lh + 1):
            file_idx = start + (i - 1)
            result.append("\n") if i > 1 else None

            if file_idx < 0 or file_idx >= count:
                # Filler: "   ~│" (cgdb draws spaces then '~' then VLINE)
                result.append(" " * lwidth, style=self.hl.style("LineNumber"))
                result.append("~",          style=self.hl.style("LineNumber"))
                result.append("│",          style="bold")
                continue

            filename = self._files[file_idx]
            # Horizontal scroll
            display_name = filename[self._sel_col:] if self._sel_col < len(filename) else ""

            if file_idx == self._sel:
                # Selected line: bold number + "→" arrow (cgdb uses '->')
                result.append(f"{file_idx + 1:{lwidth}d}",
                               style="bold " + self.hl.style("SelectedLineNr"))
                result.append("->",
                               style="bold " + self.hl.style("SelectedLineArrow"))
                result.append(display_name,
                               style=self.hl.style("SelectedLineHighlight"))
            else:
                # Normal line: number + bold "│" + space + filename
                result.append(f"{file_idx + 1:{lwidth}d}",
                               style=self.hl.style("LineNumber"))
                result.append("│", style="bold")
                result.append(" " + display_name)

        # ── Last row: status bar / search prompt ──
        result.append("\n")
        bar_style = self.hl.style("StatusLine")
        if self._search_active:
            pfx = "/" if self._search_forward else "?"
            bar = f"{pfx}{self._search_buf}"
        else:
            bar = f" {self._sel + 1}/{count}  j/k=move  Enter=open  /=search  q=quit"
        result.append(bar[:w].ljust(w), style=bar_style)

        return result

    # ------------------------------------------------------------------
    # Key handling (mirrors filedlg_recv_char)
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key  = event.key
        char = event.character or ""

        if self._search_active:
            self._handle_search_key(key, char)
            event.stop()
            return

        # 'gg' → goto top
        if self._await_g:
            self._await_g = False
            if char == "g":
                self._sel = 0; self._num_buf = ""; self.refresh()
                event.stop(); return
            self._num_buf = ""

        if char.isdigit() and char != "0":
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
            self._sel = self._clamp((count - 1) if self._num_buf == "" and count != 1
                                    else len(self._files) - 1)
            self.refresh()
        elif char == "g":
            self._await_g = True
        elif key == "slash":
            self._search_active  = True
            self._search_forward = True
            self._search_buf     = ""
            self.refresh()
        elif key == "question_mark":
            self._search_active  = True
            self._search_forward = False
            self._search_buf     = ""
            self.refresh()
        elif char == "n":
            self._search(self._search_pattern, self._search_forward)
        elif char == "N":
            self._search(self._search_pattern, not self._search_forward)
        elif key in ("enter", "return"):
            if 0 <= self._sel < len(self._files):
                self.post_message(FileSelected(self._files[self._sel]))
        event.stop()

    def _handle_search_key(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.refresh()
        elif key in ("enter", "return"):
            self._search_active  = False
            self._search_pattern = self._search_buf
            self._search(self._search_pattern, self._search_forward)
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.refresh()
        elif char and char.isprintable():
            self._search_buf += char
            # Incremental search while typing (cgdb regex_search mode)
            self._search(self._search_buf, self._search_forward)
            self.refresh()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class FileSelected(Message):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

class FileDialogClosed(Message):
    pass
