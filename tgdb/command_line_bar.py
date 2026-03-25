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
from rich.text import Text

from .highlight_groups import HighlightGroups

# Matches ":python << EOF" / ":py << MYMARKER" (heredoc trigger)
_HEREDOC_RE = re.compile(r'^(python|py)\s+<<\s+(\S+)\s*$', re.IGNORECASE)


def _pad_crop(text: str, w: int) -> str:
    """Return *text* truncated or space-padded to exactly *w* characters."""
    if len(text) >= w:
        return text[:w]
    return text + " " * (w - len(text))


class CommandLineBar(Widget):
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
        self._mode: str = "GDB"
        self._message: str = ""
        self._input_active: bool = False
        self._input_buf: str = ""
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self.can_focus = True

        # ── Multiline (heredoc) input state ──────────────────────────
        self._ml_active: bool = False
        self._ml_buf: list[str] = []        # lines collected so far
        self._ml_marker: str = ""           # e.g. "EOF"
        self._ml_cmd: str = ""              # e.g. "python"

        # ── Multiline message display state ──────────────────────────
        self._msg_lines: list[str] = []
        self._msg_scroll: int = 0           # index of first visible line
        self._msg_visible_rows: int = 0     # how many content rows are shown

        # ── Tab completion state ──────────────────────────────────────
        self._completion_provider = completion_provider
        self._completions: list[str] = []
        self._completion_idx: int = 0
        self._completion_arg_start: int = 0

        # ── Command history ───────────────────────────────────────────
        self._history: list[str] = []       # all recorded commands (oldest first)
        self._history_idx: int = -1         # -1 = not browsing; 0 = oldest
        self._history_prefix: str = ""      # prefix typed before Up was pressed
        self._history_file: Optional[Path] = history_file

        # ── Cursor position (within _input_buf) ──────────────────────
        self._cursor_pos: int = 0           # 0 = before first char; len(buf) = after last

        # ── Async task state ─────────────────────────────────────────
        # While a command task is running the bar is locked; all key input
        # is blocked and streaming output from the task is shown.
        self._task_running: bool = False
        self._streaming_buf: str = ""       # accumulated output from running task

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
        # Clear any lingering message so the input line is unobstructed
        self._message = ""
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
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

    def lock_for_task(self) -> None:
        """Called by the app when a command task starts.  Locks the bar."""
        self._task_running = True
        self._streaming_buf = ""
        self._input_active = False
        self._message = ""
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0
        self._set_height(1)
        self.refresh()

    def append_output(self, chunk: str) -> None:
        """Append streaming output from a running task and trigger a repaint."""
        if not chunk:
            return
        self._streaming_buf += chunk
        self.refresh()

    def finish_task(self) -> None:
        """Called by the app when the task ends; resets running state."""
        self._task_running = False
        self._streaming_buf = ""
        self.refresh()

    # ------------------------------------------------------------------
    # Command history — public API
    # ------------------------------------------------------------------

    def load_history(self) -> None:
        """Load command history from the history file (called at startup)."""
        if not self._history_file:
            return
        try:
            lines = self._history_file.read_text(encoding="utf-8").splitlines()
            self._history = [ln for ln in lines if ln.strip()]
        except (OSError, UnicodeDecodeError):
            self._history = []

    def save_history(self, path: Optional[Path] = None, *, max_size: int = 1024) -> Optional[str]:
        """Save the current session history to *path* (default: history file).

        Returns an error string on failure, or None on success.
        """
        target = path or self._history_file
        if target is None:
            return "history: no history file configured"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            entries = self._history[-max_size:] if max_size > 0 else self._history
            target.write_text("\n".join(entries) + "\n", encoding="utf-8")
        except OSError as exc:
            return f"history: cannot write '{target}': {exc}"
        return None

    def _add_to_history(self, cmd: str, *, save: bool = False, max_size: int = 1024) -> None:
        """Add *cmd* to in-memory history (deduplicating adjacent identical entries)."""
        cmd = cmd.strip()
        if not cmd:
            return
        if self._history and self._history[-1] == cmd:
            return
        self._history.append(cmd)
        if max_size > 0:
            self._history = self._history[-max_size:]
        if save:
            self.save_history(max_size=max_size)

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
                    self._cancel_multiline()
                    self.post_message(CommandSubmit(f"{cmd}\n{code}"))
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
                self._start_multiline(m.group(1), m.group(2))
            else:
                self.post_message(CommandSubmit(cmd))

        elif key in ("backspace", "ctrl+h"):
            self._commit_history_browse()  # adopt retrieved entry, don't revert
            if self._cursor_pos > 0:
                self._input_buf = (
                    self._input_buf[: self._cursor_pos - 1]
                    + self._input_buf[self._cursor_pos:]
                )
                self._cursor_pos -= 1
            if not self._input_buf:
                self._input_active = False
                self.post_message(CommandCancel())
            self.refresh()

        elif char and char.isprintable():
            self._commit_history_browse()  # adopt retrieved entry, don't revert
            self._input_buf = (
                self._input_buf[: self._cursor_pos]
                + char
                + self._input_buf[self._cursor_pos:]
            )
            self._cursor_pos += 1
            self.refresh()

        else:
            return False

        return True

    # ------------------------------------------------------------------
    # History navigation helpers
    # ------------------------------------------------------------------

    def _history_up(self) -> None:
        """Move to the previous history entry that matches the current prefix."""
        if not self._history:
            return
        if self._history_idx == -1:
            # Starting a new browse — save the current input as prefix
            self._history_prefix = self._input_buf
            search_start = len(self._history) - 1
        else:
            search_start = self._history_idx - 1

        for i in range(search_start, -1, -1):
            if self._history[i].startswith(self._history_prefix):
                self._history_idx = i
                self._input_buf = self._history[i]
                self._cursor_pos = len(self._input_buf)
                self.refresh()
                return

    def _history_down(self) -> None:
        """Move to the next history entry that matches the current prefix."""
        if self._history_idx == -1:
            return
        for i in range(self._history_idx + 1, len(self._history)):
            if self._history[i].startswith(self._history_prefix):
                self._history_idx = i
                self._input_buf = self._history[i]
                self._cursor_pos = len(self._input_buf)
                self.refresh()
                return
        # Past the end — restore original input
        self._reset_history_browse()
        self.refresh()

    def _reset_history_browse(self) -> None:
        """Abort browsing: revert _input_buf to the original prefix."""
        if self._history_idx != -1:
            self._input_buf = self._history_prefix
            self._cursor_pos = len(self._input_buf)
            self._history_idx = -1
            self._history_prefix = ""

    def _commit_history_browse(self) -> None:
        """Stop browsing but KEEP the current retrieved entry as _input_buf."""
        self._history_idx = -1
        self._history_prefix = ""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _start_multiline(self, cmd: str, marker: str) -> None:
        self._input_active = False
        self._ml_active = True
        self._ml_cmd = cmd.lower()
        self._ml_marker = marker
        self._ml_buf = []
        self._input_buf = ""
        self._set_height(2)     # header row + current-input row
        self.refresh()

    def _cancel_multiline(self) -> None:
        self._ml_active = False
        self._ml_buf = []
        self._ml_marker = ""
        self._ml_cmd = ""
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

    def _handle_tab(self) -> None:
        if self._completions:
            self._completion_idx = (self._completion_idx + 1) % len(self._completions)
            self._apply_completion()
        else:
            self._trigger_completion()

    def _trigger_completion(self) -> None:
        if not self._completion_provider:
            return
        buf = self._input_buf
        stripped = buf.rstrip()
        if len(stripped) < len(buf):
            arg_lead = ""
            arg_lead_start = len(buf)
        else:
            last_space = buf.rfind(" ")
            if last_space == -1:
                arg_lead = buf
                arg_lead_start = 0
            else:
                arg_lead = buf[last_space + 1:]
                arg_lead_start = last_space + 1
        try:
            candidates = self._completion_provider(arg_lead, buf, len(buf))
        except Exception:
            return
        if not candidates:
            return
        self._completions = candidates
        self._completion_idx = 0
        self._completion_arg_start = arg_lead_start
        self._apply_completion()

    def _apply_completion(self) -> None:
        if not self._completions:
            return
        cand = self._completions[self._completion_idx]
        self._input_buf = self._input_buf[:self._completion_arg_start] + cand
        self._cursor_pos = len(self._input_buf)
        self.refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w = max(10, self.size.width or 80)
        style = self.hl.style("CommandLine")

        if self._task_running:
            return self._render_streaming(w, style)

        if self._ml_active:
            return self._render_ml_input(w, style)

        if self._msg_lines:
            return self._render_msg(w, style)

        # ── Single-line modes ─────────────────────────────────────────
        if self._input_active:
            return self._render_input(w, style)

        if self._search_active:
            pfx = "/" if self._search_forward else "?"
            t = Text(_pad_crop(f"{pfx}{self._search_buf}", w), no_wrap=True, overflow="crop")
            t.stylize(style)
            return t

        if self._message:
            t = Text(_pad_crop(self._message, w), style=style, no_wrap=True, overflow="crop")
            return t

        return Text(" " * w, style=style, no_wrap=True, overflow="crop")

    def _render_input(self, w: int, style: str) -> Text:
        """Render the active command-input line with a blinking block cursor."""
        prefix = ":"
        buf = self._input_buf
        cursor = max(0, min(self._cursor_pos, len(buf)))

        full = prefix + buf
        cursor_in_full = len(prefix) + cursor

        # Scroll the view so the cursor is always visible
        if cursor_in_full >= w:
            start = cursor_in_full - w + 1
        else:
            start = 0

        # Visible slice, padded to exactly w chars
        visible = full[start:]
        if len(visible) < w:
            visible += " " * (w - len(visible))
        else:
            visible = visible[:w]

        cursor_col = cursor_in_full - start  # column of cursor in visible slice

        # Build cursor style: toggle 'reverse' so the cursor always contrasts
        # with the surrounding text. If the bar style already uses 'reverse'
        # (the default), removing it from the cursor makes it stand out as a
        # "normal" cell against the reversed background.  If the bar style does
        # not use reverse, adding it creates the same contrast.
        style_tokens = style.lower().split()
        if "reverse" in style_tokens:
            other_tokens = [t for t in style_tokens if t != "reverse"]
            cursor_style = " ".join(other_tokens) if other_tokens else "default"
        else:
            cursor_style = f"reverse {style}" if style and style != "default" else "reverse"

        t = Text(no_wrap=True, overflow="crop")
        if cursor_col > 0:
            t.append(visible[:cursor_col], style)
        ch = visible[cursor_col] if cursor_col < len(visible) else " "
        t.append(ch, cursor_style)
        after_col = cursor_col + 1
        if after_col < len(visible):
            t.append(visible[after_col:], style)
        return t

    def _render_streaming(self, w: int, style: str) -> Text:
        """Render task-running state: show the last output line plus an indicator."""
        buf = self._streaming_buf
        if buf:
            last_line = buf.rstrip("\n").rsplit("\n", 1)[-1]
            text = _pad_crop(f"\u25b6 {last_line}", w)
        else:
            text = _pad_crop("\u25b6 Running\u2026", w)
        return Text(text, style=style, no_wrap=True, overflow="crop")

    def _render_ml_input(self, w: int, style: str) -> Text:
        """Render the heredoc continuation prompt (multi-row)."""
        lines = []
        lines.append(_pad_crop(f":python << {self._ml_marker}", w))
        for ln in self._ml_buf:
            lines.append(_pad_crop(f"  {ln}", w))
        # Current input row — append a block cursor marker
        lines.append(_pad_crop(f"  {self._input_buf}\u258f", w))
        t = Text("\n".join(lines))
        t.stylize(style)
        return t

    def _render_msg(self, w: int, style: str) -> Text:
        """Render the visible window of the scrollable message display."""
        visible = max(1, self._msg_visible_rows)
        window = self._msg_lines[self._msg_scroll: self._msg_scroll + visible]
        lines = [_pad_crop(ln, w) for ln in window]

        # Pad blank rows if the window is taller than remaining content
        while len(lines) < visible:
            lines.append(" " * w)

        at_end = self._msg_scroll + visible >= len(self._msg_lines)
        if at_end:
            hint = "-- Press ENTER or type command to continue --"
        else:
            hint = "-- Use j/k to scroll more lines --"
        lines.append(_pad_crop(hint, w))

        t = Text("\n".join(lines))
        t.stylize(style)
        return t

    # ------------------------------------------------------------------
    # Key handling when the widget has focus
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self.feed_key(event.key, event.character or ""):
            event.stop()


class CommandSubmit(Message):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command


class CommandCancel(Message):
    pass


class MessageDismissed(Message):
    """Posted when the multiline message display is dismissed (Enter/ESC/q).

    Distinct from CommandCancel (which is for cancelling active CMD input)
    so that stale dismissal events don't kill a CMD session entered during
    map replay after the message was already cleared.
    """
    pass
