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

import os

from textual.widget import Widget

from .keys import FileDialogKeyMixin
from .messages import FileDialogClosed, FileSelected
from .search import FileDialogSearchMixin
from .view import FileDialogViewMixin
from ..highlight_groups import HighlightGroups


class FileDialog(FileDialogKeyMixin, FileDialogSearchMixin, FileDialogViewMixin, Widget):
    """Full-screen file picker matching cgdb's file-dialog behavior.

    Public interface
    ----------------
    ``FileDialog(hl, **kwargs)``
        Create the widget.

    ``files = [...]``
        Publish a new source-file list. The setter deduplicates entries, drops
        missing files unless they are special ``*`` items, and sorts the result
        in cgdb-compatible order.

    ``open()``, ``open_pending()``, ``close()``
        Control visibility and the async-loading placeholder state.

    ``is_open``
        Query whether the dialog is currently visible.

    Callers should treat the widget as a black box. Once the file list and
    config flags are set, the widget owns selection, search, navigation, and
    emits ``FileSelected`` / ``FileDialogClosed`` for user actions.
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
        self._body_message: str | None = None
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
        if self._files:
            self._sel = 0
        else:
            self._sel = -1
        self._sel_col = 0
        self._query_pending = False
        if self._files:
            self._body_message = None
        else:
            self._body_message = self._EMPTY_MESSAGE
        self.refresh()


    @property
    def is_open(self) -> bool:
        return self.has_class("visible")


    def _reset_interaction(self) -> None:
        if self._files:
            self._sel = 0
        else:
            self._sel = -1
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
