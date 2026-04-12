"""
Internal source-pane content widget.

This module holds ``_SourceContent``, the heavy-lifting implementation behind
the public ``SourceView`` wrapper. Splitting it out keeps ``pane.py`` focused on
the public interface while the content widget owns file loading, navigation,
search, marks, rendering, and key handling.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from textual.widget import Widget

from ..highlight_groups import HighlightGroups
from .data import SourceFile
from .file_ops import SourceFileMixin
from .keys import SourceKeyMixin
from .navigation import SourceNavigationMixin
from .rendering import SourceViewRendering
from .search import SourceSearchMixin

if TYPE_CHECKING:
    from .pane import SourceView


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
        self._count_buf: str = ""         # numeric repeat-count being typed (e.g. "20" before 'j')
        self._g_pressed: bool = False     # True after first 'g' keypress (waiting for 'gg')
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
