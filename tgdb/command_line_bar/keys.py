"""Key-handling helpers for ``CommandLineBar``."""

from __future__ import annotations

import re

from textual import events

from .messages import CommandCancel, CommandSubmit, MessageDismissed

_HEREDOC_RE = re.compile(r"^(python|py)\s+<<\s+(\S+)\s*$", re.IGNORECASE)


class CommandLineKeyMixin:
    """Mixin providing keystroke routing for ``CommandLineBar``."""

    def feed_key(self, key: str, char: str) -> bool:
        if self._task_running:
            return key != "ctrl+c"

        if self._msg_lines:
            return self._handle_message_key(key)

        if self._ml_active:
            return self._handle_multiline_key(key, char)

        return self._handle_single_line_key(key, char)


    def _handle_message_key(self, key: str) -> bool:
        if key in ("j", "down"):
            self._msg_scroll_down()
            return True
        if key in ("k", "up"):
            self._msg_scroll_up()
            return True
        self.dismiss_message()
        self.post_message(MessageDismissed())
        return True


    def _handle_multiline_key(self, key: str, char: str) -> bool:
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
                self._collapse_to_single_line()
                self.refresh()
                self.post_message(CommandCancel())
                return True
            if key == "up":
                self._history_up()
                return True
            if key == "down":
                self._history_down()
                return True
            return True

        if key == "escape":
            self._cancel_multiline()
            self.post_message(CommandCancel())
            return True

        if key in ("enter", "return"):
            line = self._input_buf
            self._input_buf = ""
            if line.strip() == self._ml_marker:
                code = "\n".join(self._ml_buf)
                cmd = self._ml_cmd
                verbatim_lines = [self._ml_header]
                verbatim_lines.extend(self._ml_buf)
                verbatim_lines.append(line)
                history_text = "\n".join(verbatim_lines)
                self._cancel_multiline()
                self.post_message(CommandSubmit(f"{cmd}\n{code}", history_text=history_text))
            else:
                self._ml_buf.append(line)
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


    def _handle_single_line_key(self, key: str, char: str) -> bool:
        if not self._input_active:
            return False

        if key == "tab":
            self._handle_tab()
            return True

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

        if key == "up":
            self._history_up()
            return True
        if key == "down":
            self._history_down()
            return True

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
            match = _HEREDOC_RE.match(cmd)
            if match:
                self._start_multiline(match.group(1), match.group(2), original=cmd)
            else:
                self.post_message(CommandSubmit(cmd))
        elif key in ("backspace", "ctrl+h"):
            self._commit_history_browse()
            if self._cursor_pos > 0:
                self._input_buf = (
                    self._input_buf[: self._cursor_pos - 1]
                    + self._input_buf[self._cursor_pos :]
                )
                self._cursor_pos -= 1
            if not self._input_buf:
                self._input_active = False
                self.post_message(CommandCancel())
            self.refresh()
        elif char and char.isprintable():
            self._commit_history_browse()
            self._input_buf = (
                self._input_buf[: self._cursor_pos]
                + char
                + self._input_buf[self._cursor_pos :]
            )
            self._cursor_pos += 1
            self.refresh()
        else:
            return False

        return True


    def _start_multiline(self, cmd: str, marker: str, *, original: str = "") -> None:
        self._input_active = False
        self._ml_active = True
        self._ml_cmd = cmd.lower()
        self._ml_marker = marker
        if original:
            self._ml_header = original
        else:
            self._ml_header = f"{cmd} << {marker}"
        self._ml_buf = []
        self._input_buf = ""
        self._set_height(2)
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
        self._collapse_to_single_line()
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


    def on_key(self, event: events.Key) -> None:
        if self.feed_key(event.key, event.character or ""):
            event.stop()
