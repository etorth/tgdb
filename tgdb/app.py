"""
Main Textual application — mirrors cgdb's interface.cpp + cgdb.cpp.

Layout:
  ┌──────────────────────────┐
  │     Source Window        │  (upper pane, CGDB mode)
  ├──────────────────────────┤
  │  [status bar]            │
  ├──────────────────────────┤
  │     GDB Window           │  (lower pane, GDB mode)
  └──────────────────────────┘

Modes:
  CGDB   — source window focused (ESC key switches here)
  GDB    — GDB terminal focused (i key)
  SCROLL — GDB terminal in scroll mode (PageUp)
  STATUS — typing a ':' command
  FILEDLG— file dialog open
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual import events
from textual.message import Message
from textual.css.query import NoMatches
from rich.text import Text

from .highlight_groups import HighlightGroups
from .key_mapper import KeyMapper
from .config import Config, ConfigParser
from .gdb_controller import GDBController, Breakpoint, Frame
from .source_widget import (
    SourceView, SourceFile,
    ToggleBreakpoint, OpenFileDialog, AwaitMarkJump, AwaitMarkSet,
    JumpGlobalMark, SearchStart, SearchUpdate, SearchCommit, SearchCancel,
    StatusMessage, ResizeSource, ToggleOrientation, OpenTTY, GDBCommand,
)
from .gdb_widget import (
    GDBWidget, ScrollModeChange,
    ScrollSearchStart, ScrollSearchUpdate, ScrollSearchCommit, ScrollSearchCancel,
)
from .status_bar import StatusBar, CommandSubmit, CommandCancel
from .file_dialog import FileDialog, FileSelected, FileDialogClosed


# ---------------------------------------------------------------------------
# Thin horizontal divider / splitter
# ---------------------------------------------------------------------------

class Divider(Static):
    DEFAULT_CSS = """
    Divider { height: 1; background: $primary-darken-2; }
    """


# ---------------------------------------------------------------------------
# The main tgdb App
# ---------------------------------------------------------------------------

class TGDBApp(App):
    """tgdb — Python front-end for GDB."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #src-pane {
        height: 1fr;
    }
    #gdb-pane {
        height: 1fr;
    }
    #status {
        height: 1;
    }
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self,
                 gdb_path: str = "gdb",
                 gdb_args: list[str] | None = None,
                 rc_file: Optional[str] = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = HighlightGroups()
        self.km = KeyMapper()
        self.cfg = Config()
        self.cp = ConfigParser(self.cfg, self.hl, self.km)
        # Register extra commands
        self._register_commands()
        # Load rc
        if rc_file:
            self.cp.load_file(rc_file)
        else:
            self.cp.load_default_rc()
        # GDB
        self.gdb = GDBController(gdb_path=gdb_path, args=gdb_args or [])
        self._gdb_task: Optional[asyncio.Task] = None
        # UI state
        self._mode: str = "GDB"          # GDB | CGDB | SCROLL | STATUS | FILEDLG
        self._cgdb_mode_key: str = "escape"
        self._await_mark_jump: bool = False
        self._await_mark_set: bool = False
        self._split_ratio: float = 0.5   # source / (source+gdb)
        self._orientation: str = "horizontal"  # horizontal | vertical
        # Source file history (per cgdb global marks need the path)
        self._source_history: list[str] = []

    # ------------------------------------------------------------------
    # Textual compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        src = SourceView(self.hl, id="src-pane")
        src.executing_line_display = self.cfg.executinglinedisplay
        src.selected_line_display = self.cfg.selectedlinedisplay
        src.tabstop = self.cfg.tabstop
        src.hlsearch = self.cfg.hlsearch
        src.ignorecase = self.cfg.ignorecase
        src.wrapscan = self.cfg.wrapscan
        src.showmarks = self.cfg.showmarks
        yield src

        status = StatusBar(self.hl, id="status")
        yield status

        gdb_w = GDBWidget(
            self.hl,
            max_scrollback=self.cfg.scrollbackbuffersize,
            id="gdb-pane"
        )
        yield gdb_w

        file_dlg = FileDialog(self.hl, id="file-dlg")
        file_dlg.ignorecase = self.cfg.ignorecase
        file_dlg.wrapscan = self.cfg.wrapscan
        yield file_dlg

    # ------------------------------------------------------------------
    # App startup
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """Wire callbacks and start GDB."""
        gdb_w = self.query_one("#gdb-pane", GDBWidget)
        gdb_w.send_to_gdb = self.gdb.send_input
        gdb_w.on_switch_to_cgdb = self._switch_to_cgdb
        gdb_w.ignorecase = self.cfg.ignorecase
        gdb_w.wrapscan = self.cfg.wrapscan

        # GDB callbacks
        self.gdb.on_console = self._on_gdb_console
        self.gdb.on_log = self._on_gdb_log
        self.gdb.on_target = self._on_gdb_console
        self.gdb.on_stopped = self._on_gdb_stopped
        self.gdb.on_running = self._on_gdb_running
        self.gdb.on_breakpoints = self._on_gdb_breakpoints
        self.gdb.on_source_files = self._on_source_files
        self.gdb.on_exit = self._on_gdb_exit
        self.gdb.on_error = self._on_gdb_error

        # Start GDB process
        try:
            rows, cols = self._terminal_size()
            self.gdb.start(rows=rows, cols=cols)
            self._gdb_task = asyncio.ensure_future(self.gdb.run_async())
        except Exception as e:
            self._show_status(f"Failed to start GDB: {e}")
            return

        # Initial mode
        self._set_mode("GDB")
        self.query_one("#gdb-pane").focus()

    def _terminal_size(self) -> tuple[int, int]:
        try:
            import shutil
            ts = shutil.get_terminal_size()
            return ts.lines, ts.columns
        except Exception:
            return 24, 80

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        status = self.query_one("#status", StatusBar)
        status.set_mode(mode)
        src = self.query_one("#src-pane", SourceView)
        # Update file info
        if src.source_file:
            fname = os.path.basename(src.source_file.path)
            status.set_file_info(fname, src.sel_line,
                                  len(src.source_file.lines))

    def _switch_to_cgdb(self) -> None:
        self._set_mode("CGDB")
        self.query_one("#src-pane").focus()

    def _switch_to_gdb(self) -> None:
        self._set_mode("GDB")
        self.query_one("#gdb-pane").focus()

    def _show_status(self, msg: str) -> None:
        self.query_one("#status", StatusBar).show_message(msg)

    # ------------------------------------------------------------------
    # Global key handling
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key = event.key

        # Await mark jump: next key is mark name
        if self._await_mark_jump:
            self._await_mark_jump = False
            char = event.character or ""
            if char == ".":
                src = self.query_one("#src-pane", SourceView)
                src.goto_executing()
            elif char == "'":
                src = self.query_one("#src-pane", SourceView)
                src.goto_last_jump()
            elif char.isalpha():
                src = self.query_one("#src-pane", SourceView)
                src.jump_to_mark(char)
            event.stop()
            return

        if self._await_mark_set:
            self._await_mark_set = False
            char = event.character or ""
            if char.isalpha():
                src = self.query_one("#src-pane", SourceView)
                src.set_mark(char)
            event.stop()
            return

        # Global CGDB-mode-key: switch to CGDB from any mode
        cgdb_key = self.cfg.cgdbmodekey.lower()
        if key.lower() == cgdb_key or key == "escape":
            if self._mode in ("GDB", "STATUS", "SCROLL"):
                self._switch_to_cgdb()
                event.stop()
                return

        # CGDB mode specific keys
        if self._mode == "CGDB":
            if key == "i":
                self._switch_to_gdb()
                event.stop()
                return
            if key == "s":
                self._switch_to_gdb()
                gdb_w = self.query_one("#gdb-pane", GDBWidget)
                gdb_w.enter_scroll_mode()
                event.stop()
                return
            if key == "colon":
                status = self.query_one("#status", StatusBar)
                status.start_command()
                self._set_mode("STATUS")
                status.focus()
                event.stop()
                return

        # GDB mode: 'i' already handled in CGDB — in GDB mode ESC switches back
        # (handled above via cgdb_mode_key)

        # Resize: F keys handled by source widget
        # Ctrl-C: send interrupt
        if key == "ctrl+c":
            self.gdb.send_interrupt()
            event.stop()
            return

    # ------------------------------------------------------------------
    # Source widget message handlers
    # ------------------------------------------------------------------

    def on_toggle_breakpoint(self, msg: ToggleBreakpoint) -> None:
        src = self.query_one("#src-pane", SourceView)
        sf = src.source_file
        if not sf:
            self._show_status("No source file loaded")
            return
        line = msg.line
        # Check if breakpoint already exists at this line
        existing = next(
            (b for b in self.gdb.breakpoints
             if b.line == line and
             (os.path.basename(b.fullname or b.file) ==
              os.path.basename(sf.path))),
            None
        )
        if existing:
            self.gdb.delete_breakpoint(existing.number)
        else:
            location = f"{sf.path}:{line}"
            self.gdb.set_breakpoint(location, temporary=msg.temporary)

    def on_open_file_dialog(self, msg: OpenFileDialog) -> None:
        self._open_file_dialog()

    def on_await_mark_jump(self, msg: AwaitMarkJump) -> None:
        self._await_mark_jump = True

    def on_await_mark_set(self, msg: AwaitMarkSet) -> None:
        self._await_mark_set = True

    def on_jump_global_mark(self, msg: JumpGlobalMark) -> None:
        src = self.query_one("#src-pane", SourceView)
        if src.load_file(msg.path):
            src.move_to(msg.line)

    def on_search_start(self, msg: SearchStart) -> None:
        status = self.query_one("#status", StatusBar)
        status.start_search(msg.forward)

    def on_search_update(self, msg: SearchUpdate) -> None:
        status = self.query_one("#status", StatusBar)
        status.update_search(msg.pattern)

    def on_search_commit(self, msg: SearchCommit) -> None:
        status = self.query_one("#status", StatusBar)
        status.cancel_input()
        self._set_mode("CGDB")

    def on_search_cancel(self, msg: SearchCancel) -> None:
        status = self.query_one("#status", StatusBar)
        status.cancel_input()
        self._set_mode("CGDB")

    def on_status_message(self, msg: StatusMessage) -> None:
        self._show_status(msg.text)

    def on_resize_source(self, msg: ResizeSource) -> None:
        if msg.percent:
            self._split_ratio = max(0.1, min(0.9,
                self._split_ratio + (msg.delta / 100)))
        else:
            self._split_ratio = max(0.1, min(0.9,
                self._split_ratio + msg.delta * 0.05))
        self._apply_split()

    def on_toggle_orientation(self, msg: ToggleOrientation) -> None:
        self.cfg.winsplitorientation = (
            "vertical" if self.cfg.winsplitorientation == "horizontal"
            else "horizontal"
        )
        self._apply_split()

    def on_gdb_command(self, msg: GDBCommand) -> None:
        self._send_gdb_cli(msg.cmd)

    # ------------------------------------------------------------------
    # GDB widget messages
    # ------------------------------------------------------------------

    def on_scroll_mode_change(self, msg: ScrollModeChange) -> None:
        if msg.active:
            self._set_mode("SCROLL")
        else:
            self._set_mode("GDB")

    def on_scroll_search_start(self, msg: ScrollSearchStart) -> None:
        status = self.query_one("#status", StatusBar)
        status.start_search(msg.forward)

    def on_scroll_search_update(self, msg: ScrollSearchUpdate) -> None:
        status = self.query_one("#status", StatusBar)
        status.update_search(msg.pattern)

    def on_scroll_search_commit(self, msg: ScrollSearchCommit) -> None:
        status = self.query_one("#status", StatusBar)
        status.cancel_input()

    def on_scroll_search_cancel(self, msg: ScrollSearchCancel) -> None:
        status = self.query_one("#status", StatusBar)
        status.cancel_input()

    # ------------------------------------------------------------------
    # Status bar command handling
    # ------------------------------------------------------------------

    def on_command_submit(self, msg: CommandSubmit) -> None:
        self._execute_command(msg.command)
        self._switch_to_cgdb()

    def on_command_cancel(self, msg: CommandCancel) -> None:
        self._switch_to_cgdb()

    def _execute_command(self, cmd: str) -> None:
        """Process a ':' command."""
        cmd = cmd.strip()
        if not cmd:
            return
        # Register extra command handlers before executing
        err = self.cp.execute(cmd)
        if err:
            self._show_status(err)
        else:
            # Sync config changes to widgets
            self._sync_config()

    def _register_commands(self) -> None:
        """Register app-level command handlers with the config parser."""
        handlers = {
            "quit": self._cmd_quit,
            "q": self._cmd_quit,
            "help": self._cmd_help,
            "edit": self._cmd_edit,
            "e": self._cmd_edit,
            "focus": self._cmd_focus,
            "insert": self._cmd_insert,
            "noh": self._cmd_noh,
            "continue": lambda _: self._send_gdb_cli("continue") or None,
            "c":       lambda _: self._send_gdb_cli("continue") or None,
            "next":    lambda _: self._send_gdb_cli("next") or None,
            "n":       lambda _: self._send_gdb_cli("next") or None,
            "nexti":   lambda _: self._send_gdb_cli("nexti") or None,
            "step":    lambda _: self._send_gdb_cli("step") or None,
            "s":       lambda _: self._send_gdb_cli("step") or None,
            "stepi":   lambda _: self._send_gdb_cli("stepi") or None,
            "finish":  lambda _: self._send_gdb_cli("finish") or None,
            "f":       lambda _: self._send_gdb_cli("finish") or None,
            "run":     lambda _: self._send_gdb_cli("run") or None,
            "r":       lambda _: self._send_gdb_cli("run") or None,
            "start":   lambda _: self._send_gdb_cli("start") or None,
            "kill":    lambda _: self._send_gdb_cli("kill") or None,
            "k":       lambda _: self._send_gdb_cli("kill") or None,
            "until":   lambda _: self._send_gdb_cli("until") or None,
            "u":       lambda _: self._send_gdb_cli("until") or None,
            "up":      lambda _: self._send_gdb_cli("up") or None,
            "down":    lambda _: self._send_gdb_cli("down") or None,
        }
        for name, fn in handlers.items():
            self.cp.register_handler(name, fn)

    def _cmd_quit(self, args: list) -> None:
        self.gdb.terminate()
        self.exit(0)
        return None

    def _cmd_help(self, args: list) -> None:
        self._show_cgdb_help()
        return None

    def _cmd_edit(self, args: list) -> None:
        src = self.query_one("#src-pane", SourceView)
        if src.source_file:
            src.load_file(src.source_file.path)
        return None

    def _cmd_focus(self, args: list) -> None:
        if args:
            target = args[0].lower()
            if target in ("gdb", "gdbw"):
                self._switch_to_gdb()
            elif target in ("src", "cgdb"):
                self._switch_to_cgdb()
        return None

    def _cmd_insert(self, args: list) -> None:
        self._switch_to_gdb()
        return None

    def _cmd_noh(self, args: list) -> None:
        self.cfg.hlsearch = False
        src = self.query_one("#src-pane", SourceView)
        src.hlsearch = False
        src.refresh()
        return None

    def _send_gdb_cli(self, cmd: str) -> None:
        self.gdb.cli_command(cmd)
        gdb_w = self.query_one("#gdb-pane", GDBWidget)
        # Echo command to console
        gdb_w.append_output(f"{cmd}\n")
        self._switch_to_gdb()

    # ------------------------------------------------------------------
    # GDB event callbacks (called from asyncio task — schedule on main loop)
    # ------------------------------------------------------------------

    def _on_gdb_console(self, text: str) -> None:
        self.call_from_thread(self._ui_append_gdb, text)

    def _on_gdb_log(self, text: str) -> None:
        if self.cfg.showdebugcommands:
            self.call_from_thread(self._ui_append_gdb, text)

    def _ui_append_gdb(self, text: str) -> None:
        try:
            gdb_w = self.query_one("#gdb-pane", GDBWidget)
            gdb_w.append_output(text, debug_color=self.cfg.debugwincolor)
        except NoMatches:
            pass

    def _on_gdb_stopped(self, frame: Frame) -> None:
        self.call_from_thread(self._ui_on_stopped, frame)

    def _ui_on_stopped(self, frame: Frame) -> None:
        """GDB stopped: update source file and executing line."""
        src = self.query_one("#src-pane", SourceView)
        if frame.fullname or frame.file:
            path = frame.fullname or frame.file
            if os.path.isfile(path):
                if not src.source_file or src.source_file.path != path:
                    src.load_file(path)
                elif self.cfg.autosourcereload:
                    src.reload_if_changed()
                src.exe_line = frame.line
                src.move_to(frame.line)
                # Update status
                status = self.query_one("#status", StatusBar)
                status.set_file_info(
                    os.path.basename(path), frame.line,
                    len(src.source_file.lines) if src.source_file else 0
                )
        # Request source files list
        self.gdb.request_source_files()

    def _on_gdb_running(self) -> None:
        self.call_from_thread(self._set_mode, "GDB")

    def _on_gdb_breakpoints(self, bps: list[Breakpoint]) -> None:
        self.call_from_thread(self._ui_set_breakpoints, bps)

    def _ui_set_breakpoints(self, bps: list[Breakpoint]) -> None:
        src = self.query_one("#src-pane", SourceView)
        src.set_breakpoints(bps)

    def _on_source_files(self, files: list[str]) -> None:
        self.call_from_thread(self._ui_set_source_files, files)

    def _ui_set_source_files(self, files: list[str]) -> None:
        try:
            dlg = self.query_one("#file-dlg", FileDialog)
            dlg.files = files
        except NoMatches:
            pass

    def _on_gdb_exit(self) -> None:
        self.call_from_thread(self._ui_gdb_exit)

    def _ui_gdb_exit(self) -> None:
        self._show_status("GDB exited.")
        self.exit(0)

    def _on_gdb_error(self, msg: str) -> None:
        self.call_from_thread(self._show_status, f"Error: {msg}")

    # ------------------------------------------------------------------
    # File dialog
    # ------------------------------------------------------------------

    def _open_file_dialog(self) -> None:
        if self.gdb.source_files:
            self.query_one("#file-dlg", FileDialog).open()
            self._set_mode("FILEDLG")
        else:
            self._show_status("No source files available")
            self.gdb.request_source_files()

    def on_file_selected(self, msg: FileSelected) -> None:
        dlg = self.query_one("#file-dlg", FileDialog)
        dlg.close()
        src = self.query_one("#src-pane", SourceView)
        src.load_file(msg.path)
        self._switch_to_cgdb()

    def on_file_dialog_closed(self, msg: FileDialogClosed) -> None:
        self.query_one("#file-dlg", FileDialog).close()
        self._switch_to_cgdb()

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _apply_split(self) -> None:
        """Reapply split ratio to source/GDB pane heights."""
        split = self.cfg.winsplit.lower()
        ratio = {
            "src_full": 0.9,
            "src_big": 0.7,
            "even": 0.5,
            "gdb_big": 0.3,
            "gdb_full": 0.1,
        }.get(split, self._split_ratio)

        src_pane = self.query_one("#src-pane")
        gdb_pane = self.query_one("#gdb-pane")
        h = self.size.height - 1  # minus status bar
        src_h = max(self.cfg.winminheight, int(h * ratio))
        gdb_h = max(self.cfg.winminheight, h - src_h)
        src_pane.styles.height = src_h
        gdb_pane.styles.height = gdb_h

    def on_resize(self, event: events.Resize) -> None:
        self._apply_split()
        if self.gdb.is_alive():
            self.gdb.resize(event.size.height, event.size.width)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _show_cgdb_help(self) -> None:
        help_text = [
            "tgdb — Python reimplementation of cgdb",
            "",
            "CGDB mode (source window):",
            "  ESC        — switch to CGDB mode",
            "  i          — switch to GDB mode",
            "  s          — enter scroll mode in GDB window",
            "  j/k        — move down/up",
            "  h/l        — move left/right",
            "  Ctrl-f/b   — page down/up",
            "  Ctrl-d/u   — half page down/up",
            "  G/gg       — go to bottom/top",
            "  H/M/L      — screen top/middle/bottom",
            "  //?        — search forward/backward",
            "  n/N        — next/previous search match",
            "  Space      — toggle breakpoint",
            "  t          — set temporary breakpoint",
            "  o          — open file dialog",
            "  m[a-z]     — set mark",
            "  '[a-z]     — jump to mark",
            "  ''         — jump to last jump location",
            "  '.         — jump to executing line",
            "  Ctrl-W     — toggle split orientation",
            "  -/=        — shrink/grow source window",
            "  F5/F6      — run/continue",
            "  F7/F8      — finish/next",
            "  F10        — step",
            "  :          — enter command mode",
            "",
            "GDB mode (gdb window):",
            "  ESC        — switch to CGDB mode",
            "  PageUp     — enter scroll mode",
            "",
            "Scroll mode:",
            "  j/k        — scroll up/down",
            "  PageUp/Dn  — page up/down",
            "  G/gg       — end/beginning",
            "  //?/n/N    — search",
            "  q/i/Enter  — exit scroll mode",
            "",
            "Commands (:set, :highlight, :map, :quit, :help, ...)",
        ]
        src = self.query_one("#src-pane", SourceView)
        sf = SourceFile("<help>", help_text)
        src.source_file = sf
        src.exe_line = 0
        src.move_to(1)
        self._switch_to_cgdb()

    # ------------------------------------------------------------------
    # Config sync
    # ------------------------------------------------------------------

    def _sync_config(self) -> None:
        """Push config values to widgets after a :set command."""
        cfg = self.cfg
        src = self.query_one("#src-pane", SourceView)
        src.executing_line_display = cfg.executinglinedisplay
        src.selected_line_display = cfg.selectedlinedisplay
        src.tabstop = cfg.tabstop
        src.hlsearch = cfg.hlsearch
        src.ignorecase = cfg.ignorecase
        src.wrapscan = cfg.wrapscan
        src.showmarks = cfg.showmarks
        src.refresh()

        gdb_w = self.query_one("#gdb-pane", GDBWidget)
        gdb_w.ignorecase = cfg.ignorecase
        gdb_w.wrapscan = cfg.wrapscan
        gdb_w.max_scrollback = cfg.scrollbackbuffersize

        self.km.timeout_ms = cfg.timeoutlen
        self.km.ttimeout_ms = cfg.ttimeoutlen
        self.km.timeout_enabled = cfg.timeout
        self.km.ttimeout_enabled = cfg.ttimeout

        self._apply_split()
