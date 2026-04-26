"""
Internal source-pane content widget.

This module holds ``_SourceContent``, the heavy-lifting implementation behind
the public ``SourceView`` wrapper. Splitting it out keeps ``pane.py`` focused on
the public interface while the content widget owns file loading, navigation,
search, marks, rendering, and key handling.
"""

import logging
import os
import re
from typing import TYPE_CHECKING

from textual.widget import Widget

from ..gdb_controller import Breakpoint
from ..highlight_groups import HighlightGroups
from .data import BP_DISABLED, BP_ENABLED, BP_NONE, SourceFile, _LOGO_LINES
from .keys import SourceKeyMixin
from .messages import SearchCancel, SearchCommit, SearchStart, SearchUpdate
from .navigation import SourceNavigationMixin
from .rendering import SourceViewRendering

if TYPE_CHECKING:
    from .pane import SourceView


_log = logging.getLogger("tgdb.source")


class SourceFileMixin:
    """Mixin providing file and breakpoint state updates for ``_SourceContent``."""

    def load_file(self, path: str) -> bool:
        try:
            previous = self.source_file
            if previous:
                self._file_positions[previous.path] = self.sel_line

            with open(path, errors="replace") as handle:
                content = handle.read()
            lines = content.expandtabs(self.tabstop).splitlines()
            if not lines:
                lines = [""]
            source_file = SourceFile(path, lines)
            if previous and previous.path == path:
                source_file.bp_flags = list(previous.bp_flags[: len(lines)])
                while len(source_file.bp_flags) < len(lines):
                    source_file.bp_flags.append(BP_NONE)
                source_file.marks_local = dict(previous.marks_local)
            self.source_file = source_file
            self._show_logo = False
            self._col_offset = 0
            target_line = self._file_positions.get(path, 1)
            self.sel_line = max(1, min(target_line, len(lines)))
            self._ensure_visible(self.sel_line)
            self.refresh()
            _log.info(f"load file: {path}")
            return True
        except OSError as exc:
            _log.warning(f"load file failed: {path}: {exc}")
            return False


    def reload_if_changed(self) -> bool:
        source_file = self.source_file
        if not source_file:
            return False
        try:
            mtime = os.path.getmtime(source_file.path)
            if mtime != source_file.mtime:
                return self.load_file(source_file.path)
        except OSError:
            pass
        return False


    def set_breakpoints(self, bps: list[Breakpoint]) -> None:
        source_file = self.source_file
        if not source_file:
            return
        source_file.bp_flags = [BP_NONE] * len(source_file.lines)
        for breakpoint_info in bps:
            fullname = breakpoint_info.fullname or breakpoint_info.file
            if not fullname:
                continue
            try:
                same_file = (
                    os.path.abspath(fullname) == os.path.abspath(source_file.path)
                    or os.path.basename(fullname) == os.path.basename(source_file.path)
                )
            except Exception:
                same_file = False
            if same_file and 1 <= breakpoint_info.line <= len(source_file.lines):
                if breakpoint_info.enabled:
                    source_file.bp_flags[breakpoint_info.line - 1] = BP_ENABLED
                else:
                    source_file.bp_flags[breakpoint_info.line - 1] = BP_DISABLED
        self.refresh()


class SourceSearchMixin:
    """Mixin providing search state and regex navigation for ``_SourceContent``."""

    def search(self, pattern: str, forward: bool = True, start: int | None = None) -> bool:
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


class _SourceContent(
    SourceFileMixin,
    SourceNavigationMixin,
    SourceSearchMixin,
    SourceKeyMixin,
    SourceViewRendering,
    Widget,
):
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
        self._pane: "SourceView" | None = None
        self.source_file: SourceFile | None = None
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
        self._count_buf: str = ""         # numeric repeat-count being typed (e.g. "20" before 'j')
        self._g_pressed: bool = False     # True after first 'g' keypress (waiting for 'gg')
        self._col_offset: int = 0  # horizontal scroll (cgdb sel_col)
        self._show_logo: bool = False  # force logo display (:logo command)
        self._file_positions: dict[str, int] = {}
        self._pending_search: tuple[str, bool] | None = None
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
