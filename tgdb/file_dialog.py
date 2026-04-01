"""
File dialog widget — mirrors cgdb's filedlg.cpp closely, with an async
loading state for slow source-file enumeration.

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
    _PENDING_MESSAGE = "file list query pending"
    _EMPTY_MESSAGE = "No source files available."

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._files: list[str] = []
        self._sel: int = 0  # 0-based selected index into _files
        self._sel_col: int = 0  # horizontal scroll (matches filedlg sel_col)
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self._search_pattern: str = ""
        self._search_origin: int = 0  # cgdb sel_rline: fixed start while typing
        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self._num_buf: str = ""
        self._await_g: bool = False
        self._query_pending: bool = False
        self._body_message: Optional[str] = None
        self.can_focus = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def files(self) -> list[str]:
        return self._files

    @files.setter
    def files(self, value: list[str]) -> None:
        # Mirror cgdb filedlg_add_file_choice():
        # - skip empty / duplicate entries
        # - skip nonexistent files unless they are special '*' entries
        # - order plain relative paths first, then './' relative paths,
        #   then absolute paths, with lexicographic ordering inside each group
        seen: set[str] = set()
        filtered: list[str] = []
        for path in value:
            if not path or path in seen:
                continue
            if not path.startswith("*") and not os.path.exists(path):
                continue
            seen.add(path)
            filtered.append(path)

        def sort_key(path: str) -> tuple[int, str]:
            if path.startswith("/"):
                group = 2
            elif path.startswith("."):
                group = 1
            else:
                group = 0
            return (group, path)

        self._files = sorted(filtered, key=sort_key)
        self._sel = 0 if self._files else -1
        self._sel_col = 0
        self._query_pending = False
        self._body_message = None if self._files else self._EMPTY_MESSAGE
        self.refresh()

    @property
    def is_open(self) -> bool:
        return self.has_class("visible")

    def _reset_interaction(self) -> None:
        self._sel = 0 if self._files else -1
        self._sel_col = 0
        self._search_active = False
        self._search_buf = ""
        self._search_pattern = ""
        self._search_origin = 0
        self._num_buf = ""
        self._await_g = False

    def open(self) -> None:
        self._reset_interaction()
        self.add_class("visible")
        self.focus()
        self.refresh()

    def open_pending(self) -> None:
        self._files = []
        self._query_pending = True
        self._body_message = self._PENDING_MESSAGE
        self._reset_interaction()
        self.add_class("visible")
        self.focus()
        self.refresh()

    def close(self) -> None:
        self._query_pending = False
        self._search_active = False
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
        count = len(self._files)
        height = self._list_height()
        if count == 0:
            return 0
        if count < height:
            return (count - height) // 2  # may be negative → clamp below
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

    def _search(self, pattern: str, forward: bool, origin: int | None = None) -> bool:
        """Search for *pattern* starting one step past *origin* (cgdb sel_rline).

        If *origin* is None, start from the current selection (used for n/N).
        During incremental search, *origin* is fixed at the line where / was
        pressed so that each keystroke always finds the first match from that
        position — matching filedlg_search_regex() behaviour exactly.
        """
        n = len(self._files)
        if n == 0 or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        start = self._sel if origin is None else origin
        order = list(range(start + 1, n)) + (list(range(0, start + 1)) if self.wrapscan else []) if forward else list(range(start - 1, -1, -1)) + (list(range(n - 1, start - 1, -1)) if self.wrapscan else [])
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
        w = self._w()
        lh = self._list_height()
        lwidth = self._lwidth()
        count = len(self._files)
        result = Text(no_wrap=True, overflow="crop")

        # ── Row 0: centred label (cgdb print_in_middle) ──
        label = self._LABEL
        pad = max(0, (w - len(label)) // 2)
        result.append(" " * pad + label, style=self.hl.style("StatusLine"))
        result.append(" " * max(0, w - pad - len(label)), style=self.hl.style("StatusLine"))
        result.append("\n")

        if self._query_pending or (count == 0 and self._body_message):
            message = self._body_message or ""
            message_row = max(0, (lh - 1) // 2)
            for i in range(lh):
                if i:
                    result.append("\n")
                if i == message_row and message:
                    pad = max(0, (w - len(message)) // 2)
                    result.append(" " * pad, style=self.hl.style("Normal"))
                    result.append(message, style=self.hl.style("StatusLine"))
                    result.append(" " * max(0, w - pad - len(message)), style=self.hl.style("Normal"))
                else:
                    result.append(" " * w, style=self.hl.style("Normal"))

            result.append("\n")
            bar_style = self.hl.style("StatusLine")
            if self._query_pending:
                bar = " Esc=quit"
            else:
                bar = " q=quit"
            result.append(bar[:w].ljust(w), style=bar_style)
            return result

        # ── Rows 1..lh: file list ──
        start = self._scroll_start()

        for i in range(1, lh + 1):
            file_idx = start + (i - 1)
            result.append("\n") if i > 1 else None

            if file_idx < 0 or file_idx >= count:
                # Filler: "   ~│" (cgdb draws spaces then '~' then VLINE)
                result.append(" " * lwidth, style=self.hl.style("LineNumber"))
                result.append("~", style=self.hl.style("LineNumber"))
                result.append("│", style="bold")
                continue

            filename = self._files[file_idx]
            # Horizontal scroll
            display_name = filename[self._sel_col :] if self._sel_col < len(filename) else ""

            if file_idx == self._sel:
                # Selected line: bold number + "→" arrow (cgdb uses '->')
                result.append(f"{file_idx + 1:{lwidth}d}", style="bold " + self.hl.style("SelectedLineNr"))
                result.append("->", style="bold " + self.hl.style("SelectedLineArrow"))
                result.append(display_name, style=self.hl.style("SelectedLineHighlight"))
            else:
                # Normal line: number + bold "│" + space + filename
                result.append(f"{file_idx + 1:{lwidth}d}", style=self.hl.style("LineNumber"))
                result.append("│", style="bold")
                result.append(" " + display_name)

        # ── Last row: status bar / search prompt ──
        result.append("\n")
        bar_style = self.hl.style("StatusLine")
        if self._search_active:
            pfx = "/" if self._search_forward else "?"
            bar = f"{pfx}{self._search_buf}"
        else:
            current = self._sel + 1 if self._sel >= 0 else 0
            bar = f" {current}/{count}  j/k=move  Enter=open  /=search  q=quit"
        result.append(bar[:w].ljust(w), style=bar_style)

        return result

    # ------------------------------------------------------------------
    # Key handling (mirrors filedlg_recv_char)
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if self._query_pending:
            if key in ("q", "escape"):
                self.post_message(FileDialogClosed())
            event.stop()
            return

        if self._search_active:
            self._handle_search_key(key, char)
            event.stop()
            return

        # 'gg' → goto top
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
            # [N]G → jump to file N (1-indexed); G alone → jump to last
            self._sel = self._clamp(count - 1 if had_count else len(self._files) - 1)
            self.refresh()
        elif char == "g":
            self._await_g = True
        elif key == "slash":
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
            self._search_origin = self._sel  # cgdb: filedlg_search_regex_init → sel_rline = sel_line
            self.refresh()
        elif key == "question_mark":
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
            self._search_origin = self._sel  # cgdb: filedlg_search_regex_init → sel_rline = sel_line
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
            # Abort: restore selection to where search started (cgdb: sel_line = sel_rline)
            self._sel = self._search_origin
            self._search_active = False
            self.refresh()
        elif key in ("enter", "return"):
            # Confirm: update origin to current match (cgdb: sel_rline = sel_line, opt==2)
            self._search_active = False
            self._search_pattern = self._search_buf
            self._search(self._search_pattern, self._search_forward, origin=self._search_origin)
            self._search_origin = self._sel  # update for subsequent n/N
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            # Re-search from origin so shorter pattern finds first match again
            self._search(self._search_buf, self._search_forward, origin=self._search_origin)
            self.refresh()
        elif char and char.isprintable():
            self._search_buf += char
            # Incremental: always search from fixed origin (cgdb sel_rline), not current match
            self._search(self._search_buf, self._search_forward, origin=self._search_origin)
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
