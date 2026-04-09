"""
Source view widget — mirrors cgdb's sources.cpp / interface.cpp source pane.

Features:
  • Syntax highlighting via Pygments
  • Vi-like navigation (j/k, G/gg, Ctrl-b/f/u/d, H/M/L)
  • Breakpoint: line number shown in bold red (enabled) / bold yellow (disabled), set with Space
  • Executing line indicator (shortarrow/longarrow/highlight/block)
  • Selected line indicator (same styles)
  • Regex search (/ ? n N) with optional hlsearch
  • Marks (m[a-z/A-Z], '[a-z/A-Z], '', '.)
  • Auto-reload on file change
  • Footer row showing the current file path
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from textual.widget import Widget
from textual import events

from .highlight_groups import HighlightGroups
from .gdb_controller import Breakpoint
from .source_rendering import SourceViewRendering
from .source_data import (  # noqa: F401 — re-exported
    SourceFile,
    BP_NONE,
    BP_ENABLED,
    BP_DISABLED,
    _token_group,
    _TOKEN_GROUPS,
    _LOGO_LINES,
)
from .source_messages import (  # noqa: F401 — re-exported
    ToggleBreakpoint,
    OpenFileDialog,
    AwaitMarkJump,
    AwaitMarkSet,
    JumpGlobalMark,
    SearchStart,
    SearchUpdate,
    SearchCommit,
    SearchCancel,
    StatusMessage,
    ResizeSource,
    ToggleOrientation,
    OpenTTY,
    ShowHelp,
    GDBCommand,
)

from .pane_base import PaneBase

_log = logging.getLogger("tgdb.source")


# ---------------------------------------------------------------------------
# Internal content widget (does the actual rendering + key handling)
# ---------------------------------------------------------------------------


class _SourceContent(SourceViewRendering, Widget):
    """Scrollable, syntax-highlighted source viewer with vi keybindings.

    This is the internal content widget hosted inside SourceView (PaneBase).
    """

    DEFAULT_CSS = """
    _SourceContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        # Set by SourceView after construction so that post_message bubbles
        # from SourceView rather than _SourceContent.  When SourceView.on_key
        # dispatches keys directly, the active_message_pump context var is
        # SourceView; messages created here would therefore have _sender ==
        # SourceView == _SourceContent._parent, which causes Textual to stop
        # propagation at SourceView and never reach TGDBApp handlers.
        self._pane: Optional["SourceView"] = None
        self.source_file: Optional[SourceFile] = None
        self.exe_line: int = 0  # 1-based; 0 = none
        self.sel_line: int = 1  # 1-based cursor
        self._scroll_top: int = 0  # 0-based first visible line
        self._last_render_h: int = 0  # track height to detect resize in render()
        self._search_pattern: str = ""
        self._search_forward: bool = True
        self._search_active: bool = False
        self._search_buf: str = ""
        self.tabstop: int = 8
        self.executing_line_display: str = "longarrow"
        self.selected_line_display: str = "block"
        self.hlsearch: bool = False
        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self.showmarks: bool = True
        self.color: bool = True  # :set color — enables/disables syntax colors
        self._global_marks: dict[str, tuple[str, int]] = {}
        self._last_jump_line: int = 1
        self._num_buf: str = ""
        self._await_g: bool = False  # true after first 'g' (for 'gg')
        self._col_offset: int = 0  # horizontal scroll (cgdb sel_col)
        self._show_logo: bool = False  # force logo display (:logo command)
        self._file_positions: dict[str, int] = {}
        self._pending_search: Optional[tuple[str, bool]] = None
        self.can_focus = False

    def post_message(self, message) -> bool:
        """Forward through the SourceView wrapper so messages bubble correctly.

        SourceView.on_key calls _content.on_key() directly, which means the
        active_message_pump context var is set to SourceView.  Any Message()
        created inside that call therefore has _sender == SourceView, which
        equals _SourceContent._parent.  Textual's _on_message logic then calls
        message.stop() before bubbling, killing propagation at SourceView and
        preventing TGDBApp handlers from ever seeing the message.

        Posting via _pane (SourceView) avoids the parent-is-sender trap: the
        message starts its journey at SourceView, whose parent is PaneContainer,
        and _sender != PaneContainer, so bubbling proceeds normally.

        Internal Textual messages (Prune, CloseMessages, etc.) must NOT be
        redirected — they must reach _SourceContent directly so Textual's
        widget lifecycle (remove_children, _message_loop_exit) works correctly.
        """
        # Only redirect application-level messages (those defined outside textual).
        # Textual-internal messages (Prune, CloseMessages, etc.) go directly to
        # this widget so that _message_loop_exit can properly shut down this task.
        if type(message).__module__.startswith("textual."):
            return super().post_message(message)
        pane = self.__dict__.get("_pane")
        if pane is not None and pane.is_attached:
            return pane.post_message(message)
        return super().post_message(message)

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def load_file(self, path: str) -> bool:
        try:
            previous = self.source_file
            if previous:
                self._file_positions[previous.path] = self.sel_line

            with open(path, errors="replace") as f:
                content = f.read()
            lines = content.expandtabs(self.tabstop).splitlines()
            if not lines:
                lines = [""]
            sf = SourceFile(path, lines)
            if previous and previous.path == path:
                sf.bp_flags = list(previous.bp_flags[: len(lines)])
                while len(sf.bp_flags) < len(lines):
                    sf.bp_flags.append(BP_NONE)
                sf.marks_local = dict(previous.marks_local)
            self.source_file = sf
            self._show_logo = False
            self._col_offset = 0
            target_line = self._file_positions.get(path, 1)
            self.sel_line = max(1, min(target_line, len(lines)))
            self._ensure_visible(self.sel_line)
            self.refresh()
            _log.info("load file: %s", path)
            return True
        except OSError as e:
            _log.warning("load file failed: %s: %s", path, e)
            return False

    def reload_if_changed(self) -> bool:
        sf = self.source_file
        if not sf:
            return False
        try:
            mtime = os.path.getmtime(sf.path)
            if mtime != sf.mtime:
                return self.load_file(sf.path)
        except OSError:
            pass
        return False

    def set_breakpoints(self, bps: list[Breakpoint]) -> None:
        sf = self.source_file
        if not sf:
            return
        sf.bp_flags = [BP_NONE] * len(sf.lines)
        for bp in bps:
            fullname = bp.fullname or bp.file
            if not fullname:
                continue
            try:
                same = os.path.abspath(fullname) == os.path.abspath(
                    sf.path
                ) or os.path.basename(fullname) == os.path.basename(sf.path)
            except Exception:
                same = False
            if same and 1 <= bp.line <= len(sf.lines):
                if bp.enabled:
                    sf.bp_flags[bp.line - 1] = BP_ENABLED
                else:
                    sf.bp_flags[bp.line - 1] = BP_DISABLED
        self.refresh()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _line_count(self) -> int:
        if self.source_file:
            return len(self.source_file.lines)
        return len(_LOGO_LINES)

    def _visible_height(self) -> int:
        return max(1, self.size.height)

    def _ensure_visible(self, line: int) -> None:
        """Mirror cgdb: keep the selected line centered when possible."""
        h = self._visible_height()
        n = self._line_count()
        idx = line - 1
        if n <= 0 or n < h:
            self._scroll_top = 0
        else:
            self._scroll_top = max(0, min(idx - h // 2, n - h))

    def move_to(self, line: int) -> None:
        n = self._line_count()
        if n:
            self.sel_line = max(1, min(line, n))
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
        """Set horizontal scroll to an absolute display-column position.

        Pass col=999999 to scroll to the end of the currently selected line.
        """
        if col >= 999999:
            if self.source_file and 1 <= self.sel_line <= len(self.source_file.lines):
                from rich.cells import cell_len as _cell_len

                line_text = self.source_file.lines[self.sel_line - 1]
                line_cells = _cell_len(line_text)
                # Account for line-number gutter (approx 4–6 cols); use width−6
                visible_w = max(1, (self.size.width or 80) - 6)
                col = max(0, line_cells - visible_w)
            else:
                col = 0
        self._col_offset = max(0, col)
        self.refresh()

    def page_up(self) -> None:
        h = self._visible_height()
        self.sel_line = max(1, self.sel_line - h)
        self._ensure_visible(self.sel_line)
        self.refresh()

    def page_down(self) -> None:
        h = self._visible_height()
        n = self._line_count()
        if n:
            self.sel_line = min(n, self.sel_line + h)
        else:
            self.sel_line = min(1, self.sel_line + h)
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
        tmp = self._last_jump_line
        self._last_jump_line = self.sel_line
        self.move_to(tmp)

    def show_logo(self) -> None:
        """Force logo display (:logo command)."""
        self._show_logo = True
        self.refresh()

    def goto_screen_top(self) -> None:
        self.move_to(self._scroll_top + 1)

    def goto_screen_middle(self) -> None:
        self.move_to(self._scroll_top + self._visible_height() // 2 + 1)

    def goto_screen_bottom(self) -> None:
        self.move_to(self._scroll_top + self._visible_height())

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self, pattern: str, forward: bool = True, start: Optional[int] = None
    ) -> bool:
        sf = self.source_file
        if sf:
            lines = sf.lines
        else:
            lines = _LOGO_LINES
        if not lines or not pattern:
            return False
        if self.ignorecase:
            flags = re.IGNORECASE
        else:
            flags = 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        n = len(lines)
        if start is not None:
            s = start - 1
        else:
            s = self.sel_line - 1
        if forward:
            if self.wrapscan:
                order = list(range(s + 1, n)) + list(range(0, s + 1))
            else:
                order = list(range(s + 1, n))
        else:
            if self.wrapscan:
                order = list(range(s - 1, -1, -1)) + list(range(n - 1, s - 1, -1))
            else:
                order = list(range(s - 1, -1, -1))
        for idx in order:
            if rx.search(lines[idx]):
                self._last_jump_line = self.sel_line
                self.move_to(idx + 1)
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

    # ------------------------------------------------------------------
    # Marks
    # ------------------------------------------------------------------

    def set_mark(self, ch: str) -> None:
        sf = self.source_file
        if not sf:
            return
        if ch.islower():
            sf.marks_local[ch] = self.sel_line
        else:
            self._global_marks[ch] = (sf.path, self.sel_line)

    def jump_to_mark(self, ch: str) -> bool:
        sf = self.source_file
        if ch.islower():
            if sf:
                line = sf.marks_local.get(ch)
            else:
                line = None
            if line is not None:
                self._last_jump_line = self.sel_line
                self.move_to(line)
                return True
        else:
            mark = self._global_marks.get(ch)
            if mark:
                path, line = mark
                if sf and sf.path == path:
                    self._last_jump_line = self.sel_line
                    self.move_to(line)
                    return True
                else:
                    self.post_message(JumpGlobalMark(path, line))
                    return True
        return False

    # ------------------------------------------------------------------
    # Rendering — render() only, no render_line() override
    # ------------------------------------------------------------------

    # Width of the line-number field (minimum 1, grows with file size)

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def handle_tgdb_key(self, key: str, char: str) -> bool:
        if self._search_active:
            self._handle_search_input(key, char)
            return True

        # 'g' double-press for gg (goto top)
        if self._await_g:
            self._await_g = False
            if char == "g":
                self._num_buf = ""
                self.goto_top()
                return True
            # Not 'gg' — treat buffered 'g' as nothing, reprocess current key
            self._num_buf = ""

        # Numeric prefix: 1-9 always starts/extends a count; 0 extends an
        # already-started count (e.g. "20j" → count 20) but alone means col-0.
        if char.isdigit() and (char != "0" or self._num_buf):
            self._num_buf += char
            return True
        has_prefix = bool(self._num_buf)
        if self._num_buf:
            count = int(self._num_buf)
        else:
            count = 1
        self._num_buf = ""

        consumed = True
        if key in ("j", "down"):
            self.scroll_down(count)
        elif key in ("k", "up"):
            self.scroll_up(count)
        elif key in ("h", "left"):
            self.scroll_col(-count)
        elif key in ("l", "right"):
            self.scroll_col(count)
        elif key in ("ctrl+f", "pagedown"):
            for _ in range(count):
                self.page_down()
        elif key in ("ctrl+b", "pageup"):
            for _ in range(count):
                self.page_up()
        elif key == "ctrl+d":
            self.half_page_down()
        elif key == "ctrl+u":
            self.half_page_up()
        elif key == "G":
            if has_prefix:
                n = self._line_count() or 1
                self.move_to(max(1, min(count, n)))
            else:
                self.goto_bottom()
        elif key == "H":
            self.goto_screen_top()
        elif key == "M":
            self.goto_screen_middle()
        elif key == "L":
            self.goto_screen_bottom()
        elif char == "g":
            self._await_g = True  # wait for second 'g'
        elif key == "slash":
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
            self.post_message(SearchStart(forward=True))
        elif key == "question_mark":
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
            self.post_message(SearchStart(forward=False))
        elif char == "n":
            if not self.search_next():
                self.post_message(StatusMessage("Pattern not found"))
        elif char == "N":
            if not self.search_prev():
                self.post_message(StatusMessage("Pattern not found"))
        elif key == "space":
            self.post_message(ToggleBreakpoint(self.sel_line))
        elif char == "t":
            self.post_message(ToggleBreakpoint(self.sel_line, temporary=True))
        elif char == "u":
            # cgdb source_input 'u': run until current cursor location
            sf2 = self.source_file
            if sf2:
                self.post_message(GDBCommand(f"until {sf2.path}:{self.sel_line}"))
        elif char == "o":
            self.post_message(OpenFileDialog())
        elif key == "colon" or char == ":":
            getattr(self.app, "_enter_cmd_mode", lambda: None)()
        elif key == "apostrophe":
            self.post_message(AwaitMarkJump())
        elif char == "m":
            self.post_message(AwaitMarkSet())
        elif key == "ctrl+l":
            self.app.refresh()
        elif key == "minus":
            self.post_message(ResizeSource(-1, rows=True))
        elif key in ("equal",) or char == "=":
            self.post_message(ResizeSource(1, rows=True))
        elif key == "underscore":
            self.post_message(ResizeSource(-1, jump=True))
        elif key == "plus":
            self.post_message(ResizeSource(1, jump=True))
        elif key == "ctrl+w":
            self.post_message(ToggleOrientation())
        elif key == "ctrl+t":
            self.post_message(OpenTTY())
        elif char == "0":
            # vim: '0' = go to beginning of line (column 0)
            self.scroll_col_to(0)
        elif char == "^":
            # vim: '^' = first visible char (same as 0 for source view)
            self.scroll_col_to(0)
        elif key == "dollar" or char == "$":
            # vim: '$' = go to end of line
            self.scroll_col_to(999999)
        elif key == "f1":
            self.post_message(ShowHelp())  # cgdb: if_display_help
        elif key == "f5":
            self.post_message(GDBCommand("run"))
        elif key == "f6":
            self.post_message(GDBCommand("continue"))
        elif key == "f7":
            self.post_message(GDBCommand("finish"))
        elif key == "f8":
            self.post_message(GDBCommand("next"))
        elif key == "f10":
            self.post_message(GDBCommand("step"))
        else:
            consumed = False

        return consumed

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if getattr(self.app, "_mode", None) == "CMD":
            from .command_line_bar import CommandLineBar

            try:
                status = self.app.query_one("#cmdline", CommandLineBar)
                status.feed_key(key, char)
            except Exception:
                pass
            event.stop()
            return

        if self.handle_tgdb_key(key, char):
            event.stop()

    def _handle_search_input(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.post_message(SearchCancel())
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            if self._search_pattern:
                if self.source_file is None and getattr(
                    self.app, "_initial_source_pending", False
                ):
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

    # ------------------------------------------------------------------
    # Hover tooltip: evaluate word under mouse cursor
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public pane wrapper
# ---------------------------------------------------------------------------

# Attributes that must be forwarded to _SourceContent on set.
_SRC_DELEGATE_SET = frozenset(
    {
        "exe_line",
        "sel_line",
        "executing_line_display",
        "selected_line_display",
        "tabstop",
        "hlsearch",
        "ignorecase",
        "wrapscan",
        "showmarks",
        "color",
    }
)


class SourceView(PaneBase):
    """Source-code pane: title bar (file path) + _SourceContent viewer."""

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _SourceContent(hl)
        self._content._pane = self  # so _content.post_message bubbles through us

    def title(self) -> Optional[str]:
        content = self.__dict__.get("_content")
        if content is not None:
            sf = getattr(content, "source_file", None)
        else:
            sf = None
        if sf is not None:
            return sf.path
        return None

    def align(self) -> str:
        return "left"

    def compose(self):
        yield from super().compose()
        yield self._content

    # ------------------------------------------------------------------
    # Key, resize, refresh delegation
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        self._content.on_key(event)

    def refresh(self, *args, **kwargs):
        # Refresh title bar in case source_file path changed.
        if self._title_bar is not None and self._title_bar.is_mounted:
            self._title_bar.refresh()
        if self._content.is_mounted:
            self._content.refresh(*args, **kwargs)
        return super().refresh(*args, **kwargs)

    # ------------------------------------------------------------------
    # Attribute delegation to _SourceContent
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value) -> None:
        if name in _SRC_DELEGATE_SET and "_content" in self.__dict__:
            setattr(self._content, name, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name: str):
        content = self.__dict__.get("_content")
        if content is not None:
            return getattr(content, name)
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
