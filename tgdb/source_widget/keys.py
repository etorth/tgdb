"""Key-handling helpers for the internal source-pane content widget."""

import logging

from textual import events

from .messages import (
    AwaitMarkJump,
    AwaitMarkSet,
    GDBCommand,
    OpenFileDialog,
    OpenTTY,
    ResizeSource,
    ShowHelp,
    StatusMessage,
    ToggleBreakpoint,
    ToggleOrientation,
)


_log = logging.getLogger("tgdb.source_widget")


class SourceKeyMixin:
    """Mixin providing source-mode key dispatch for ``_SourceContent``."""

    def _handle_movement_key(self, key: str, char: str, count: int, has_prefix: bool) -> bool:
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
                line_count = self._line_count() or 1
                self.move_to(min(count, line_count))
            else:
                self.goto_bottom()
        elif key == "H":
            self.goto_screen_top()
        elif key == "M":
            self.goto_screen_middle()
        elif key == "L":
            self.goto_screen_bottom()
        elif char == "0" and not has_prefix:
            self.scroll_col_to(0)
        elif char == "^":
            self.scroll_col_to(0)
        elif key == "dollar" or char == "$":
            self.scroll_col_to(999999)
        else:
            return False
        return True


    def _handle_search_key(self, key: str, char: str) -> bool:
        if key == "slash":
            self._start_search(forward=True)
        elif key == "question_mark":
            self._start_search(forward=False)
        elif char == "n":
            if not self.search_next():
                self.post_message(StatusMessage("Pattern not found"))
        elif char == "N":
            if not self.search_prev():
                self.post_message(StatusMessage("Pattern not found"))
        else:
            return False
        return True


    def _handle_breakpoint_key(self, key: str, char: str) -> bool:
        if key == "space":
            self.post_message(ToggleBreakpoint(self.sel_line))
        elif char == "t":
            self.post_message(ToggleBreakpoint(self.sel_line, temporary=True))
        else:
            return False
        return True


    def _handle_pane_and_gdb_key(self, key: str, char: str) -> bool:
        if char == "u":
            source_file = self.source_file
            if source_file:
                self.post_message(GDBCommand(f"until {source_file.path}:{self.sel_line}"))
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
        elif key == "equal" or char == "=":
            self.post_message(ResizeSource(1, rows=True))
        elif key == "underscore":
            self.post_message(ResizeSource(-1, jump=True))
        elif key == "plus":
            self.post_message(ResizeSource(1, jump=True))
        elif key == "ctrl+w":
            self.post_message(ToggleOrientation())
        elif key == "ctrl+t":
            self.post_message(OpenTTY())
        elif key == "f1":
            self.post_message(ShowHelp())
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
            return False
        return True


    def handle_tgdb_key(self, key: str, char: str) -> bool:
        if self._search_active:
            self._handle_search_input(key, char)
            return True

        if self._g_pressed:
            self._g_pressed = False
            if char == "g":
                self._count_buf = ""
                self.goto_top()
                return True
            self._count_buf = ""

        if char.isdigit() and (char != "0" or self._count_buf):
            self._count_buf += char
            return True
        has_prefix = bool(self._count_buf)
        count = int(self._count_buf) if self._count_buf else 1
        self._count_buf = ""

        if self._handle_movement_key(key, char, count, has_prefix):
            return True
        if self._handle_search_key(key, char):
            return True
        if self._handle_breakpoint_key(key, char):
            return True
        if char == "g":
            self._g_pressed = True
            return True
        return self._handle_pane_and_gdb_key(key, char)


    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if getattr(self.app, "_mode", None) == "CMD":
            from ..command_line_bar import CommandLineBar

            try:
                status = self.app.query_one("#cmdline", CommandLineBar)
                status.feed_key(key, char)
            except Exception:
                _log.debug("feed_key to cmdline failed", exc_info=True)
            event.stop()
            return

        if self.handle_tgdb_key(key, char):
            event.stop()
