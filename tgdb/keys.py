"""Key dispatch helpers for the application package."""

import logging
from typing import TYPE_CHECKING

from textual import events
from textual.css.query import NoMatches

from .command_line_bar import CommandLineBar, CommandSubmit
from .local_variable_pane import LocalVariablePane

if TYPE_CHECKING:
    from .main import TGDBApp


_log = logging.getLogger("tgdb.clipboard")
_clipboard_warned: set[str] = set()


def _warn_clipboard_once(app: "TGDBApp", op: str, exc: BaseException) -> None:
    """Log a clipboard error the first time each (op,kind) combination occurs.

    *op* is "copy" or "paste".  Errors are silenced after the first report so
    the log is not spammed when the user repeats the operation, but the user
    still gets a single hint about why their clipboard request did nothing.
    """
    kind = type(exc).__name__
    key = f"{op}:{kind}"
    if key in _clipboard_warned:
        return
    _clipboard_warned.add(key)
    if isinstance(exc, ImportError):
        hint = "pyperclip is not installed; run `pip install pyperclip`"
    else:
        hint = f"pyperclip {op} failed: {exc}"
    _log.warning(hint)
    try:
        app.notify(hint, severity="warning", timeout=4.0)
    except Exception:
        pass


def _copy_clipboard(app: "TGDBApp", text: str) -> None:
    """Copy *text* to the system clipboard.

    Tries pyperclip first; falls back to Textual's OSC 52 mechanism.  A
    one-shot warning is surfaced if pyperclip is missing or unusable so the
    user knows why nothing landed in the system clipboard.
    """
    try:
        import pyperclip

        pyperclip.copy(text)
        return
    except Exception as exc:
        _warn_clipboard_once(app, "copy", exc)
    app.copy_to_clipboard(text)


def _read_clipboard(app: "TGDBApp") -> str:
    """Read text from the system clipboard.

    Tries pyperclip first; falls back to Textual's local clipboard (holds
    whatever was last copied within the app via OSC 52).  A one-shot warning
    is surfaced if pyperclip is missing or unusable.
    """
    try:
        import pyperclip

        return pyperclip.paste()
    except Exception as exc:
        _warn_clipboard_once(app, "paste", exc)
    return app.clipboard


class KeyRoutingMixin:
    """Key dispatch and input routing."""

    def _handle_pending_mark_key(self: "TGDBApp", char: str) -> bool:
        src = self._get_source_view(mounted_only=True)
        if src is None:
            self._await_mark_jump = False
            self._await_mark_set = False
            return False

        if self._await_mark_jump:
            self._await_mark_jump = False
            if char == ".":
                src.goto_executing()
            elif char == "'":
                src.goto_last_jump()
            elif char.isalpha():
                src.jump_to_mark(char)
            return True

        if self._await_mark_set:
            self._await_mark_set = False
            if char.isalpha():
                src.set_mark(char)
            return True

        return False


    def _handle_tgdb_mode_key(self: "TGDBApp", key: str, char: str) -> bool:
        if self._mode != "TGDB":
            return False

        # Consult the TGDB-mode key mapper (unless we're already replaying a map)
        if not self._in_map_replay:
            result = self.km.feed("tgdb", key)
            if result == []:
                # Buffering — key consumed but no action yet
                return True
            if result != [key]:
                # The map fired — replay the expansion
                self._replay_key_sequence(result)
                return True
            # result == [key]: no map matched, fall through to normal handling

        src = self._get_source_view(mounted_only=True)

        if src is not None and src.handle_tgdb_key(key, char):
            return True

        # ":" enters CMD mode even when source pane is absent
        if key == "colon" or char == ":":
            self._enter_cmd_mode()
            return True

        if key == "i":
            self._switch_to_gdb()
            return True
        if key == "s":
            self._switch_to_gdb()
            gdb_w = self._get_gdb_widget(mounted_only=True)
            if gdb_w is not None:
                gdb_w.enter_scroll_mode()
            return True

        return False


    def _replay_key_sequence(self: "TGDBApp", tokens: list[str]) -> None:
        """Dispatch a list of key-name tokens as if the user typed them."""
        self._in_map_replay = True
        try:
            for i, token in enumerate(tokens):
                if self._dispatch_key_internal(token):
                    remaining = tokens[i + 1 :]
                    if remaining:
                        self._pending_replay_tokens.extend(remaining)
                    break
        finally:
            self._in_map_replay = False


    def _resume_pending_replay(self: "TGDBApp") -> None:
        if not self._pending_replay_tokens:
            return
        tokens = self._pending_replay_tokens
        self._pending_replay_tokens = []
        self._replay_key_sequence(tokens)

    # ------------------------------------------------------------------
    # imap support — GDB-mode key mapper
    # ------------------------------------------------------------------

    def _imap_feed(self: "TGDBApp", key: str) -> "list[str] | None":
        """Feed one key token to the GDB-mode mapper.

        Returns:
        - ``None``  : token buffered (no result yet)
        - ``[key]`` : no imap matched — send key as-is
        - other list: imap expansion — replay instead of forwarding
        """
        if self._in_map_replay:
            return [key]
        result = self.km.feed("gdb", key)
        if result == []:
            return None  # still buffering
        return result


    def _replay_gdb_key_sequence(self: "TGDBApp", tokens: list[str]) -> None:
        """Replay an imap expansion directly into the GDB PTY."""
        gdb_w = self._get_gdb_widget(mounted_only=True)
        for token in tokens:
            # Special tokens that need escape sequences
            if gdb_w:
                raw = gdb_w._KEY_BYTES.get(token)
            else:
                raw = None
            if raw:
                self.gdb.send_input(raw)
            else:
                # Derive char from token
                if len(token) == 1 and token.isprintable():
                    char = token
                elif token == "space":
                    char = " "
                elif token == "enter":
                    char = "\n"
                elif token == "tab":
                    char = "\t"
                elif token == "backspace":
                    char = "\x08"
                elif token == "escape":
                    char = "\x1b"
                elif token.startswith("ctrl+") and len(token) == 6:
                    # Clamp to the C0 range — ``ctrl+5`` etc. would otherwise
                    # compute ``chr(-11)`` and ValueError out of the replay
                    # loop, leaking a partial map expansion to the GDB PTY.
                    code = ord(token[5].upper()) - 64
                    char = chr(code) if 0 <= code <= 31 else ""
                else:
                    char = ""
                if char:
                    self.gdb.send_input(char.encode())


    def _dispatch_key_internal(self: "TGDBApp", key: str) -> bool:
        """Route a key-name token through the mode-aware dispatch stack.

        Used when replaying a key-map expansion.  Returns True when replay
        should pause and queue the remaining tokens (e.g. a replayed <CR>
        that submits an async command task).
        """
        # Derive character: a single printable char is its own character
        if len(key) == 1 and key.isprintable():
            char = key
        elif key == "space":
            char = " "
        else:
            char = ""

        # ESC / tgdb-mode key → switch to TGDB (already in TGDB = no-op)
        tgdb_key = self.cfg.tgdbmodekey.lower()
        if key == "escape" or key.lower() == tgdb_key:
            if self._mode in ("GDB_PROMPT", "CMD", "GDB_SCROLL", "ML_MESSAGE"):
                self._switch_to_tgdb()
            return False

        if self._mode == "ML_MESSAGE":
            return self._dispatch_ml_message_key(key, char)

        if self._mode == "CMD":
            return self._dispatch_cmd_mode_key(key, char)

        if self._mode == "TGDB":
            return self._dispatch_tgdb_replay_key(key, char)

        # GDB_PROMPT mode — forward char or Enter to the terminal
        if char:
            self.gdb.send_input(char.encode())
        elif key == "enter":
            self.gdb.send_input(b"\n")
        return False


    def _dispatch_ml_message_key(self: "TGDBApp", key: str, char: str) -> bool:
        """Forward a key to the CommandLineBar while in ML_MESSAGE mode."""
        try:
            bar = self.query_one("#cmdline", CommandLineBar)
            bar.feed_key(key, char)
            # If the message was dismissed (non-scroll), sync to TGDB mode so
            # the next replay token sees the correct mode.
            if not bar._msg_lines:
                self._switch_to_tgdb()
        except NoMatches:
            pass
        return False


    def _dispatch_cmd_mode_key(self: "TGDBApp", key: str, char: str) -> bool:
        """Route a key while in CMD mode.  Returns True if replay should pause."""
        try:
            bar = self.query_one("#cmdline", CommandLineBar)
        except NoMatches:
            return False
        if key in ("enter", "return"):
            cmd = bar._input_buf
            bar._reset_history_browse()
            bar._input_active = False
            bar._input_buf = ""
            bar.refresh()
            # Post CommandSubmit so the command goes through the async task
            # path (_run_cmd_task / execute_async) — same as manual Enter.
            # This is important for :python blocks which need 'await' support.
            if cmd.strip():
                self.post_message(CommandSubmit(cmd))
                return True  # pause replay until task finishes
            elif self._mode == "CMD":
                self._switch_to_tgdb()
        else:
            bar.feed_key(key, char)
        return False


    def _dispatch_tgdb_replay_key(self: "TGDBApp", key: str, char: str) -> bool:
        """Route a key while in TGDB mode (replay path)."""
        src = self._get_source_view(mounted_only=True)
        if src is not None and src.handle_tgdb_key(key, char):
            return False
        # ':' enters CMD mode even when the source pane is absent
        if key == "colon" or char == ":":
            self._enter_cmd_mode()
            return False
        if key == "i":
            self._switch_to_gdb()
            return False
        if key == "s":
            self._switch_to_gdb()
            gdb_w = self._get_gdb_widget(mounted_only=True)
            if gdb_w is not None:
                gdb_w.enter_scroll_mode()
        return False


    def _handle_non_gdb_focus_key(self: "TGDBApp", key: str, char: str) -> bool:
        """Absorb keys that arrive at GDB during focus handoff to CGDB/STATUS."""
        if self._handle_pending_mark_key(char):
            return True

        if self._mode == "CMD":
            try:
                self.query_one("#cmdline", CommandLineBar).feed_key(key, char)
            except NoMatches:
                pass
            return True

        if self._mode == "ML_MESSAGE":
            try:
                status = self.query_one("#cmdline", CommandLineBar)
                if not status.feed_key(key, char):
                    status.dismiss_message()
                    self._switch_to_tgdb()
            except NoMatches:
                pass
            return True

        if self._mode == "TGDB":
            self._handle_tgdb_mode_key(key, char)
            return True

        return False

    # ------------------------------------------------------------------
    # Global key handling
    # ------------------------------------------------------------------

    def _point_in_widget(self: "TGDBApp", widget, screen_x: int, screen_y: int) -> bool:
        if widget is None:
            return False
        if not getattr(widget, "is_mounted", False):
            return False
        if not getattr(widget, "display", True):
            return False
        try:
            region = widget.region
        except Exception:
            return False
        if region.width <= 0 or region.height <= 0:
            return False
        return (
            region.x <= screen_x < region.x + region.width
            and region.y <= screen_y < region.y + region.height
        )


    def on_key(self: "TGDBApp", event: events.Key) -> None:
        key = event.key
        char = event.character or ""
        menu = self._get_context_menu()

        if self._file_dialog_pending:
            # Let the file dialog widget consume the key directly.
            return

        if menu and menu.is_open:
            if key == "escape":
                self._close_context_menu()
            event.stop()
            return

        if self._handle_pending_mark_key(char):
            event.stop()
            return

        # Ctrl-C: cancel running command task first; otherwise interrupt GDB.
        # Must be checked before the CMD-mode block because feed_key() swallows
        # all keys while _task_running is True.
        if key == "ctrl+c":
            if self._cmd_task is not None and not self._cmd_task.done():
                self._cmd_task.cancel()
                event.stop()
                return
            self.gdb.send_interrupt()
            event.stop()
            return

        # ESC / tgdb mode key → switch to TGDB from GDB_PROMPT/CMD/GDB_SCROLL/ML_MESSAGE
        tgdb_key = self.cfg.tgdbmodekey.lower()
        if key == "escape" or key.lower() == tgdb_key:
            if self._mode in ("GDB_PROMPT", "CMD", "GDB_SCROLL"):
                self._switch_to_tgdb()
                event.stop()
                return

        if self._mode == "ML_MESSAGE":
            try:
                status = self.query_one("#cmdline", CommandLineBar)
                status.feed_key(key, char)
                event.stop()
                if not status._msg_lines:
                    self._switch_to_tgdb()
            except NoMatches:
                pass
            return

        if self._mode == "CMD":
            try:
                status = self.query_one("#cmdline", CommandLineBar)
                if status.feed_key(key, char):
                    event.stop()
                    return
            except NoMatches:
                pass

        if self._handle_tgdb_mode_key(key, char):
            event.stop()
            return


    def on_mouse_down(self: "TGDBApp", event: events.MouseDown) -> None:
        menu = self._get_context_menu()
        screen_x = int(event.screen_x)
        screen_y = int(event.screen_y)

        if event.button == 3:
            # Close an open context menu if the right-click lands outside it.
            if (
                menu
                and menu.is_open
                and not self._context_menu_contains(screen_x, screen_y)
            ):
                self._close_context_menu(restore_focus=False)
            try:
                clicked_widget, _ = self.get_widget_at(screen_x, screen_y)
            except Exception:
                clicked_widget = event.widget
            target = self._find_workspace_item(clicked_widget)

            try:
                selected = self.screen.get_selected_text()
            except (IndexError, Exception):
                self.screen.clear_selection()
                selected = None
            if selected:
                # Selected text anywhere → copy to system clipboard.
                _copy_clipboard(self, selected)
                self.screen.clear_selection()
                event.stop()
                return

            if target is self._gdb_widget and self._mode == "GDB_PROMPT":
                # No selection + GDB prompt → paste from system clipboard.
                text = _read_clipboard(self)
                if text:
                    self.gdb.send_input(text)
                event.stop()
                return

            cmdline = self._get_cmdline()
            if (
                cmdline is not None
                and self._mode in ("CMD", "ML_MESSAGE")
                and self._point_in_widget(cmdline, screen_x, screen_y)
            ):
                # No selection + right-click on the command line bar in input
                # mode → paste from system clipboard into the bar.
                text = _read_clipboard(self)
                if text:
                    cmdline.feed_paste(text)
                event.stop()
                return

            if target is not None:
                self._context_menu_target = target
                # If the right-click landed on a LocalVariablePane, remember
                # which tree node (if any) was hit so the context menu can
                # offer node-specific actions.
                if isinstance(target, LocalVariablePane):
                    self._locals_context_node = target._get_node_at_screen(
                        screen_x, screen_y
                    )
                else:
                    self._locals_context_node = None
                self._open_context_menu(screen_x, screen_y)
                event.stop()
                return

        if menu and menu.is_open and event.button == 1:
            if not self._context_menu_contains(screen_x, screen_y):
                self._close_context_menu()
                event.stop()
