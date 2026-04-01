"""History management and tab-completion mixin for CommandLineBar."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


class HistoryMixin:
    """Mixin providing command history and tab-completion for CommandLineBar.

    All instance attributes referenced here are initialised in
    ``CommandLineBar.__init__``.
    """

    # ------------------------------------------------------------------
    # Command history — public API
    # ------------------------------------------------------------------

    def load_history(self) -> None:
        """Load command history from the history file at startup.

        The file format matches the tgdb rc file format: plain commands on
        single lines, multi-line Python blocks as ``python << MARKER`` heredocs.
        Entries are stored verbatim (header + body + closing marker).
        """
        if not self._history_file:
            return
        try:
            raw_lines = self._history_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self._history = []
            return

        entries: list[str] = []
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i]
            stripped = line.strip()
            i += 1
            if not stripped:
                continue
            # Heredoc entry: "python << EOF" … "EOF"
            m = re.match(r'^(python|py)\s+<<\s+(\S+)\s*$', stripped, re.IGNORECASE)
            if m:
                marker = m.group(2)
                # Collect verbatim: header + body lines + closing marker
                entry_lines: list[str] = [line.rstrip()]
                while i < len(raw_lines):
                    code_line = raw_lines[i]
                    i += 1
                    entry_lines.append(code_line.rstrip())
                    if code_line.strip() == marker:
                        break
                entries.append("\n".join(entry_lines))
            else:
                entries.append(stripped)
        self._history = entries

    def save_history(self, path: Optional[Path] = None, *, max_size: int = 1024) -> Optional[str]:
        """Save the current session history to *path* (default: history file).

        Multi-line (heredoc) entries are stored verbatim (header + body + marker).
        max_size=0 writes an empty file (history buffer disabled).
        Returns an error string on failure, None on success.
        """
        target = path or self._history_file
        if target is None:
            return "history: no history file configured"

        if max_size == 0:
            entries: list[str] = []
        elif max_size > 0:
            entries = self._history[-max_size:]
        else:
            entries = list(self._history)

        file_lines: list[str] = []
        for entry in entries:
            # Entries are stored verbatim — multiline entries already contain
            # the full heredoc (header + body + marker).
            file_lines.extend(entry.splitlines())

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(file_lines)
            if content:
                content += "\n"
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"history: cannot write '{target}': {exc}"
        return None

    def list_history(self) -> Optional[str]:
        """Return a numbered listing of all history entries for :history command."""
        if not self._history:
            return "No history entries."

        lines: list[str] = []
        idx_w = len(str(len(self._history)))
        for i, entry in enumerate(self._history, 1):
            prefix = f"{i:>{idx_w}} "
            if "\n" in entry:
                # Multi-line (heredoc) entry — show verbatim with indented continuation
                entry_lines = entry.splitlines()
                lines.append(f"{prefix}{entry_lines[0]}")
                indent = " " * (idx_w + 1) + "   "
                for cont_line in entry_lines[1:]:
                    lines.append(f"{indent}{cont_line}")
            else:
                lines.append(f"{prefix}{entry}")
        return "\n".join(lines)

    def _add_to_history(self, cmd: str, *, max_size: int = 1024) -> None:
        """Add *cmd* to in-memory history.

        If max_size == 0, history is disabled and nothing is recorded.
        Adjacent identical entries are deduplicated (but each entry is unique
        since time-stamped comment delimiters are always distinct).
        """
        cmd = cmd.strip()
        if not cmd:
            return
        if max_size == 0:
            return  # history buffer disabled
        if self._history and self._history[-1] == cmd:
            return  # suppress adjacent duplicates
        self._history.append(cmd)
        if max_size > 0:
            self._history = self._history[-max_size:]

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
                entry = self._history[i]
                if "\n" in entry:
                    self._show_history_multiline(entry)
                else:
                    self._cancel_history_multiline()
                    self._input_buf = entry
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
                entry = self._history[i]
                if "\n" in entry:
                    self._show_history_multiline(entry)
                else:
                    self._cancel_history_multiline()
                    self._input_buf = entry
                    self._cursor_pos = len(self._input_buf)
                self.refresh()
                return
        # Past the end — restore original input
        self._cancel_history_multiline()
        self._reset_history_browse()
        self.refresh()

    def _show_history_multiline(self, entry: str) -> None:
        """Switch into multiline display mode for a recalled heredoc history entry."""
        all_lines = entry.splitlines()
        self._ml_active = True
        self._ml_history_recall = True
        self._ml_history_full = entry
        self._ml_cmd = ""
        self._ml_marker = ""
        self._ml_buf = all_lines  # all verbatim lines
        self._input_buf = ""
        self._input_active = False
        # all lines of the entry
        self._set_height(len(all_lines))

    def _cancel_history_multiline(self) -> None:
        """Exit history-recall multiline display mode."""
        if self._ml_history_recall:
            self._ml_active = False
            self._ml_history_recall = False
            self._ml_history_full = ""
            self._ml_buf = []
            self._ml_marker = ""
            self._ml_cmd = ""
            self._input_buf = ""
            self._input_active = True
            self._set_height(1)

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
    # Tab completion
    # ------------------------------------------------------------------

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
