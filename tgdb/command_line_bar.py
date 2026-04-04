"""
Dedicated bottom command-line bar for ':' commands, search prompts, and messages.

Normally renders as one row.  Expands vertically during:
- Heredoc multi-line :python << MARKER input (collection phase)
- Multi-line command output / error display (dismiss phase)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from textual.widget import Widget
from textual.message import Message
from textual import events

from .highlight_groups import HighlightGroups

# Matches ":python << EOF" / ":py << MYMARKER" (heredoc trigger)
_HEREDOC_RE = re.compile(r"^(python|py)\s+<<\s+(\S+)\s*$", re.IGNORECASE)


from .cmdline_history import HistoryMixin
from .cmdline_render import RenderMixin


class CommandLineBar(HistoryMixin, RenderMixin, Widget):
    """Command-line bar at the bottom of the screen.

    Normally 1 row.  Expands to multiple rows while:
    - A heredoc :python << MARKER block is being typed
    - A multi-line result/error from a command is being displayed
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
        self._ml_history_recall: bool = False  # True when showing a recalled heredoc entry
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

    # ------------------------------------------------------------------
    # Height management
    # ------------------------------------------------------------------

    def _set_height(self, n: int) -> None:
        self.styles.height = max(1, n)

    # ------------------------------------------------------------------
    # State setters (called by app)
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.refresh()

    def show_message(self, msg: str) -> None:
        """Show a single-line status message (collapses any multiline display)."""
        self._message = msg
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
        self._set_height(1)
        self.refresh()

    def show_multiline_message(self, msg: str) -> None:
        """Expand the bar to display a scrollable multi-line message.

        All lines are stored; the bar height is capped at half the terminal
        height.  j/k scroll the window; Enter/ESC/q dismiss.
        """
        try:
            max_rows = max(5, self.app.size.height // 2)
        except Exception:
            max_rows = 12

        all_lines = msg.splitlines()
        # Actual bar height: content rows + 1 hint row, capped at max_rows
        actual_height = min(len(all_lines) + 1, max_rows)
        actual_height = max(2, actual_height)

        self._msg_lines = all_lines
        self._msg_scroll = 0
        self._msg_visible_rows = actual_height - 1  # rows for content

        self._message = ""
        self._set_height(actual_height)
        self.refresh()

    def dismiss_message(self) -> None:
        """Collapse the multi-line message and restore the bar to 1 row."""
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
        self._message = ""
        self._set_height(1)
        self.refresh()

    def start_command(self) -> None:
        # Clear any lingering message/async-print so the input line is unobstructed
        self._message = ""
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
        self._streaming_buf = ""  # clear fire-and-forget async-print display
        self._input_active = True
        self._search_active = False
        self._input_buf = ""
        self._cursor_pos = 0
        # Reset history browsing
        self._history_idx = -1
        self._history_prefix = ""
        self._set_height(1)
        self.refresh()

    def start_search(self, forward: bool) -> None:
        self._search_active = True
        self._search_forward = forward
        self._search_buf = ""
        self._input_active = False
        self._message = ""
        self.refresh()

    def update_search(self, pattern: str) -> None:
        self._search_buf = pattern
        self.refresh()

    def cancel_input(self) -> None:
        self._input_active = False
        self._search_active = False
        self._ml_active = False
        self._ml_buf = []
        self._message = ""
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
        self._set_height(1)
        self.refresh()

    # ------------------------------------------------------------------
    # Async task support — lock bar and stream output while task runs
    # ------------------------------------------------------------------

    def lock_for_task(self) -> int:
        """Called by the app when a command task starts.  Locks the bar.

        Returns the task generation number for use with ``append_output``.
        """
        self._task_gen += 1
        self._task_running = True
        self._streaming_buf = ""
        self._collected_lines = []
        self._input_active = False
        self._message = ""
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
        self._set_height(1)
        self.refresh()
        return self._task_gen

    def append_output(self, chunk: str, *, task_gen: int = 0) -> None:
        """Append streaming output from a running task (async-print-op).

        *task_gen* identifies which task produced the output.  Only output
        whose *task_gen* matches the current ``_task_gen`` is buffered in
        ``_collected_lines`` for the sync-print-op.  Stale output from
        fire-and-forget coroutines left by earlier tasks is still displayed
        (async-print-op) but never pollutes the current task's sync buffer.
        """
        if not chunk:
            return

        # Buffer only if this output belongs to the current task
        is_current = task_gen == self._task_gen
        if is_current:
            raw = chunk.rstrip("\n")
            if raw:
                self._collected_lines.extend(raw.split("\n"))

        if self._task_running:
            # Replace streaming buf with latest chunk for async-print display
            self._streaming_buf = chunk
            display_lines = chunk.rstrip("\n").split("\n")
            self._set_height(max(1, len(display_lines)))
            self.refresh()
        else:
            # Fire-and-forget: show if bar is idle
            if self._can_show_async_print():
                self._streaming_buf = chunk
                display_lines = chunk.rstrip("\n").split("\n")
                self._set_height(max(1, len(display_lines)))
                self.refresh()

    def _can_show_async_print(self) -> bool:
        """Return True if the bar can show an async-print-op right now.

        Conditions where async-print is suppressed:
        - User is typing input (_input_active or _ml_active)
        - A sync-print-op message is displayed (_msg_lines)
        """
        if self._input_active or self._ml_active:
            return False
        if self._msg_lines:
            return False
        return True

    def get_collected_output(self) -> list[str]:
        """Return all output lines collected during the task and clear the buffer."""
        lines = list(self._collected_lines)
        self._collected_lines = []
        return lines

    def finish_task(self) -> None:
        """Called by the app when the task ends; resets running state."""
        self._task_running = False
        self._streaming_buf = ""
        self._set_height(1)
        self.refresh()

    # ------------------------------------------------------------------
    # Command history — public API
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Key dispatch
    # ------------------------------------------------------------------

    def set_completion_provider(self, provider: Callable) -> None:
        """Set callable for Tab completion: (arg_lead, cmd_line, cursor_pos) -> list[str]."""
        self._completion_provider = provider

    def feed_key(self, key: str, char: str) -> bool:
        """Handle one keystroke.  Returns True if the key was consumed."""

        # While a command task is running all keyboard input is blocked,
        # except Ctrl+C which must propagate to TGDBApp.on_key for task
        # cancellation (feed_key is called from CommandLineBar.on_key which
        # fires first because the bar has focus; returning False lets the
        # event bubble up so the app can call _cmd_task.cancel()).
        if self._task_running:
            return key != "ctrl+c"

        # ── Multiline message dismiss ─────────────────────────────────
        if self._msg_lines:
            if key in ("j", "down"):
                self._msg_scroll_down()
                return True
            if key in ("k", "up"):
                self._msg_scroll_up()
                return True
            # Any other key (including enter/ESC) dismisses the message.
            self.dismiss_message()
            self.post_message(MessageDismissed())
            return True

        # ── Heredoc continuation input ────────────────────────────────
        if self._ml_active:
            # History-recalled multiline entry — read-only display
            if self._ml_history_recall:
                if key in ("enter", "return"):
                    full_cmd = self._ml_history_full
                    self._cancel_history_multiline()
                    self._reset_history_browse()
                    self.post_message(CommandSubmit(full_cmd))
                    return True
                if key == "escape":
                    self._cancel_history_multiline()
                    self._reset_history_browse()
                    self._input_active = False
                    self._set_height(1)
                    self.refresh()
                    self.post_message(CommandCancel())
                    return True
                if key == "up":
                    self._history_up()
                    return True
                if key == "down":
                    self._history_down()
                    return True
                return True  # swallow other keys in recall mode

            if key == "escape":
                self._cancel_multiline()
                self.post_message(CommandCancel())
                return True

            if key in ("enter", "return"):
                line = self._input_buf
                self._input_buf = ""
                if line.strip() == self._ml_marker:
                    # Terminator found — assemble and submit the block
                    code = "\n".join(self._ml_buf)
                    cmd = self._ml_cmd
                    # Build verbatim history text: header + body lines + closing marker
                    header = self._ml_header
                    verbatim_lines = [header] + list(self._ml_buf) + [line]
                    history_text = "\n".join(verbatim_lines)
                    self._cancel_multiline()
                    self.post_message(CommandSubmit(f"{cmd}\n{code}", history_text=history_text))
                else:
                    self._ml_buf.append(line)
                    # header + collected + current-input row
                    self._set_height(2 + len(self._ml_buf))
                self.refresh()
                return True

            if key in ("backspace", "ctrl+h"):
                self._input_buf = self._input_buf[:-1]
                self.refresh()
                return True

            if char and char.isprintable():
                self._input_buf += char
                self.refresh()
                return True

            return False

        # ── Normal single-line command input ──────────────────────────
        if not self._input_active:
            return False

        if key == "tab":
            self._handle_tab()
            return True

        # Cursor movement (readline-style)
        if key in ("left", "ctrl+b"):
            if self._cursor_pos > 0:
                self._cursor_pos -= 1
                self.refresh()
            return True
        if key in ("right", "ctrl+f"):
            if self._cursor_pos < len(self._input_buf):
                self._cursor_pos += 1
                self.refresh()
            return True
        if key in ("home", "ctrl+a"):
            self._cursor_pos = 0
            self.refresh()
            return True
        if key in ("end", "ctrl+e"):
            self._cursor_pos = len(self._input_buf)
            self.refresh()
            return True

        # History navigation — Up/Down arrows with prefix matching
        if key == "up":
            self._history_up()
            return True
        if key == "down":
            self._history_down()
            return True

        # Any non-Tab, non-history key clears the active completion cycle
        if self._completions:
            self._completions = []
            self._completion_idx = 0

        if key == "escape":
            self._reset_history_browse()
            self._input_active = False
            self.post_message(CommandCancel())

        elif key in ("enter", "return"):
            cmd = self._input_buf
            self._reset_history_browse()
            self._input_active = False
            self._input_buf = ""
            self.refresh()
            m = _HEREDOC_RE.match(cmd)
            if m:
                self._start_multiline(m.group(1), m.group(2), original=cmd)
            else:
                self.post_message(CommandSubmit(cmd))

        elif key in ("backspace", "ctrl+h"):
            self._commit_history_browse()  # adopt retrieved entry, don't revert
            if self._cursor_pos > 0:
                self._input_buf = self._input_buf[: self._cursor_pos - 1] + self._input_buf[self._cursor_pos :]
                self._cursor_pos -= 1
            if not self._input_buf:
                self._input_active = False
                self.post_message(CommandCancel())
            self.refresh()

        elif char and char.isprintable():
            self._commit_history_browse()  # adopt retrieved entry, don't revert
            self._input_buf = self._input_buf[: self._cursor_pos] + char + self._input_buf[self._cursor_pos :]
            self._cursor_pos += 1
            self.refresh()

        else:
            return False

        return True

    # ------------------------------------------------------------------
    # History navigation helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _start_multiline(self, cmd: str, marker: str, *, original: str = "") -> None:
        self._input_active = False
        self._ml_active = True
        self._ml_cmd = cmd.lower()
        self._ml_marker = marker
        self._ml_header = original if original else f"{cmd} << {marker}"
        self._ml_buf = []
        self._input_buf = ""
        self._set_height(2)  # header row + current-input row
        self.refresh()

    def _cancel_multiline(self) -> None:
        self._ml_active = False
        self._ml_history_recall = False
        self._ml_history_full = ""
        self._ml_buf = []
        self._ml_marker = ""
        self._ml_cmd = ""
        self._ml_header = ""
        self._input_buf = ""
        self._set_height(1)
        self.refresh()

    def _msg_scroll_down(self) -> None:
        max_scroll = max(0, len(self._msg_lines) - self._msg_visible_rows)
        if self._msg_scroll < max_scroll:
            self._msg_scroll += 1
            self.refresh()

    def _msg_scroll_up(self) -> None:
        if self._msg_scroll > 0:
            self._msg_scroll -= 1
            self.refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Key handling when the widget has focus
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self.feed_key(event.key, event.character or ""):
            event.stop()


class CommandSubmit(Message):
    def __init__(self, command: str, *, history_text: str = "") -> None:
        super().__init__()
        self.command = command
        self.history_text = history_text  # verbatim input for history (if different from command)


class CommandCancel(Message):
    pass


class MessageDismissed(Message):
    """Posted when the multiline message display is dismissed (Enter/ESC/q).

    Distinct from CommandCancel (which is for cancelling active CMD input)
    so that stale dismissal events don't kill a CMD session entered during
    map replay after the message was already cleared.
    """

    pass
