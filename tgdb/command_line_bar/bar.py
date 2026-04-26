"""
Dedicated bottom command-line bar for ':' commands, search prompts, and messages.

Normally renders as one row.  Expands vertically during:
- Heredoc multi-line :python << MARKER input (collection phase)
- Multi-line command output / error display (dismiss phase)
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from textual.widget import Widget

from .history import HistoryMixin
from .keys import CommandLineKeyMixin
from .messages import CommandCancel, CommandSubmit, MessageDismissed
from .render import RenderMixin
from .state import CommandLineStateMixin
from .task import CommandLineTaskMixin
from ..highlight_groups import HighlightGroups


class CommandLineBar(
    CommandLineKeyMixin,
    CommandLineTaskMixin,
    CommandLineStateMixin,
    HistoryMixin,
    RenderMixin,
    Widget,
):
    """Bottom command/status bar used for command mode, search, and messages.

    Public interface
    ----------------
    ``CommandLineBar(hl, completion_provider=None, history_file=None, **kwargs)``
        Create the widget.

    ``set_mode(mode)``
        Update the externally visible mode label.

    ``start_command()``, ``start_search(forward)``, ``update_search(pattern)``,
    ``cancel_input()``
        Drive the active prompt state from the app layer.

    ``show_message(msg)``, ``show_multiline_message(msg)``,
    ``dismiss_message()``
        Display transient status/error output.

    ``lock_for_task()``, ``append_output(chunk)``, ``get_collected_output()``,
    ``finish_task()``
        Drive the async command-task output path.

    ``set_completion_provider(provider)`` and ``feed_key(key, char)``
        Integrate completion and keystroke routing.

    ``load_history()``, ``save_history(...)``, ``list_history()``
        Manage persistent command history using tgdb's heredoc-aware history
        format.

    Callers should treat the widget as a black box. Once constructed, the app
    only needs to publish state through the methods above and handle the
    ``CommandSubmit``, ``CommandCancel``, and ``MessageDismissed`` messages that
    the widget emits.
    """

    DEFAULT_CSS = """
    CommandLineBar {
        height: 1;
        background: $primary-darken-2;
    }
    """

    def __init__(
        self,
        hl: HighlightGroups,
        completion_provider: Optional[Callable] = None,
        history_file: Optional[Path] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._mode: str = "GDB_PROMPT"
        self._message: str = ""
        self._input_active: bool = False
        self._input_buf: str = ""
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self.can_focus = True

        # ── Multiline (heredoc) input state ──────────────────────────
        self._ml_active: bool = False
        self._ml_buf: list[str] = []  # lines collected so far
        self._ml_marker: str = ""  # e.g. "EOF"
        self._ml_cmd: str = ""  # e.g. "python"
        self._ml_header: str = ""  # original header as typed
        self._ml_history_recall: bool = (
            False  # True when showing a recalled heredoc entry
        )
        self._ml_history_full: str = ""  # full multiline command for recall

        # ── Multiline message display state ──────────────────────────
        self._msg_lines: list[str] = []
        self._msg_scroll: int = 0  # index of first visible line
        self._msg_visible_rows: int = 0  # how many content rows are shown

        # ── Tab completion state ──────────────────────────────────────
        self._completion_provider = completion_provider
        self._completions: list[str] = []
        self._completion_idx: int = 0
        self._completion_arg_start: int = 0

        # ── Tab-completion popup state ───────────────────────────────
        # When more than one candidate is available a floating
        # ``CompletionPopup`` widget is opened by the app in response to a
        # ``CompletionPopupShow`` message.  While ``_popup_active`` is True
        # the bar intercepts Tab/Shift-Tab/Enter/Escape to drive the popup
        # instead of normal command-line editing.
        self._popup_active: bool = False
        self._popup_orig_buf: str = ""  # input text before popup opened
        self._popup_orig_cursor: int = 0  # cursor position before popup

        # ── Command history ───────────────────────────────────────────
        self._history: list[str] = []  # all recorded commands (oldest first)
        self._history_idx: int = -1  # -1 = not browsing; 0 = oldest
        self._history_prefix: str = ""  # prefix typed before Up was pressed
        self._history_file: Optional[Path] = history_file

        # ── Cursor position (within _input_buf) ──────────────────────
        self._cursor_pos: int = 0  # 0 = before first char; len(buf) = after last

        # ── Async task state ─────────────────────────────────────────
        # While a command task is running the bar is locked; all key input
        # is blocked and streaming output from the task is shown.
        self._task_running: bool = False
        self._streaming_buf: str = ""  # accumulated output from running task
        self._collected_lines: list[str] = []  # all output lines collected during task
        self._task_gen: int = 0  # generation counter for task isolation
