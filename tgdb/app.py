"""
Main Textual application — mirrors cgdb's interface.cpp + cgdb.cpp.

Layout (horizontal split, default):
  ┌──────────────────────────┐
  │     Source Window        │  upper pane — CGDB mode
  ├──────────────────────────┤
  │  status bar              │  1 line
  ├──────────────────────────┤
  │     GDB Window           │  lower pane — GDB mode
  └──────────────────────────┘

Modes: CGDB | GDB | SCROLL | STATUS | FILEDLG
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual import events
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
from .status_bar import StatusBar, CommandSubmit, CommandCancel, DragResize
from .file_dialog import FileDialog, FileSelected, FileDialogClosed


class TGDBApp(App):
    """tgdb — Python front-end for GDB, compatible with cgdb."""

    CSS = """
    Screen {
        layers: base dialog;
        layout: vertical;
    }
    #split-container {
        layer: base;
        layout: vertical;
        height: 1fr;
        width: 1fr;
    }
    /* #src-col wraps source pane + status bar in vertical split */
    #src-col {
        layout: vertical;
        height: 1fr;
        min-width: 4;
    }
    #src-pane {
        height: 1fr;
        min-height: 2;
    }
    #status {
        layer: base;
        height: 1;
        width: 1fr;
    }
    #gdb-pane {
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #file-dlg {
        layer: dialog;
        width: 1fr;
        height: 1fr;
        display: none;
        background: $surface;
    }
    #file-dlg.visible {
        display: block;
    }
    """

    def __init__(self, gdb_path: str = "gdb",
                 gdb_args: list[str] | None = None,
                 rc_file: Optional[str] = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = HighlightGroups()
        self.km = KeyMapper()
        self.cfg = Config()
        self.cp = ConfigParser(self.cfg, self.hl, self.km)
        self._register_commands()
        if rc_file:
            self.cp.load_file(rc_file)
        else:
            self.cp.load_default_rc()

        self.gdb = GDBController(gdb_path=gdb_path, args=gdb_args or [])
        self._gdb_task: Optional[asyncio.Task] = None

        self._mode: str = "GDB"
        self._await_mark_jump: bool = False
        self._await_mark_set: bool = False
        self._split_ratio: float = 0.5
        self._file_dialog_pending: bool = False

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Widget(id="split-container"):
            with Widget(id="src-col"):
                yield SourceView(self.hl, id="src-pane")
                yield StatusBar(self.hl, id="status")
            yield GDBWidget(self.hl, max_scrollback=self.cfg.scrollbackbuffersize,
                            id="gdb-pane")
        yield FileDialog(self.hl, id="file-dlg")

    # ------------------------------------------------------------------
    # on_mount — async so asyncio.create_task works
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        # Configure source widget
        src = self.query_one("#src-pane", SourceView)
        src.executing_line_display = self.cfg.executinglinedisplay
        src.selected_line_display  = self.cfg.selectedlinedisplay
        src.tabstop    = self.cfg.tabstop
        src.hlsearch   = self.cfg.hlsearch
        src.ignorecase = self.cfg.ignorecase
        src.wrapscan   = self.cfg.wrapscan
        src.showmarks  = self.cfg.showmarks

        # Configure GDB widget
        gdb_w = self.query_one("#gdb-pane", GDBWidget)
        gdb_w.ignorecase        = self.cfg.ignorecase
        gdb_w.wrapscan          = self.cfg.wrapscan
        gdb_w.send_to_gdb       = self.gdb.send_input        # bytes → primary PTY
        gdb_w.resize_gdb        = self.gdb.resize             # keep pyte in sync
        gdb_w.on_switch_to_cgdb = self._switch_to_cgdb

        # Configure file dialog
        fd = self.query_one("#file-dlg", FileDialog)
        fd.ignorecase = self.cfg.ignorecase
        fd.wrapscan   = self.cfg.wrapscan

        # GDB callbacks — on_console now delivers raw bytes from GDB's PTY,
        # fed directly into the pyte VT100 emulator (matching cgdb's libvterm).
        self.gdb.on_console     = lambda data: self.call_later(gdb_w.feed_bytes, data)
        self.gdb.on_stopped     = lambda f: self.call_later(self._ui_on_stopped, f)
        self.gdb.on_running     = lambda:   self.call_later(self._ui_on_running)
        self.gdb.on_breakpoints = lambda b: self.call_later(self._ui_set_breakpoints, b)
        self.gdb.on_source_files= lambda f: self.call_later(self._ui_set_source_files, f)
        self.gdb.on_source_file = lambda f: self.call_later(self._ui_load_source_file, f)
        self.gdb.on_exit        = lambda:   self.call_later(self._ui_gdb_exit)
        self.gdb.on_error       = lambda m: self.call_later(self._show_status, f"Error: {m}")

        # Start GDB process
        try:
            self.gdb.start(rows=40, cols=200)
        except Exception as e:
            self._show_status(f"Failed to start GDB: {e}")
            return

        # Start async read loop
        self._gdb_task = asyncio.create_task(self.gdb.run_async())
        asyncio.ensure_future(self._request_initial_source())

        # Initial mode: GDB focused
        self._set_mode("GDB")
        gdb_w.focus()

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        try:
            status = self.query_one("#status", StatusBar)
            status.set_mode(mode)
            self._update_status_file_info()
        except NoMatches:
            pass
        # cgdb: scr_refresh(gdb_scroller, focus==GDB, ...) — hide GDB cursor when not focused
        try:
            gdb_w = self.query_one("#gdb-pane", GDBWidget)
            gdb_w.gdb_focused = (mode in ("GDB", "SCROLL"))
            gdb_w.refresh()
        except NoMatches:
            pass

    def _switch_to_cgdb(self) -> None:
        self._set_mode("CGDB")
        try:
            self.query_one("#src-pane").focus()
        except NoMatches:
            pass

    def _switch_to_gdb(self) -> None:
        self._set_mode("GDB")
        try:
            self.query_one("#gdb-pane").focus()
        except NoMatches:
            pass

    def _show_status(self, msg: str) -> None:
        try:
            self.query_one("#status", StatusBar).show_message(msg)
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # Global key handling
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        key  = event.key
        char = event.character or ""

        # Awaiting mark jump — next keypress is the mark letter
        if self._await_mark_jump:
            self._await_mark_jump = False
            if char == ".":
                self.query_one("#src-pane", SourceView).goto_executing()
            elif char == "'":
                self.query_one("#src-pane", SourceView).goto_last_jump()
            elif char.isalpha():
                self.query_one("#src-pane", SourceView).jump_to_mark(char)
            event.stop(); return

        # Awaiting mark set — next keypress is the mark letter
        if self._await_mark_set:
            self._await_mark_set = False
            if char.isalpha():
                self.query_one("#src-pane", SourceView).set_mark(char)
            event.stop(); return

        # ESC / cgdb mode key → switch to CGDB from GDB/STATUS/SCROLL
        cgdb_key = self.cfg.cgdbmodekey.lower()
        if key == "escape" or key.lower() == cgdb_key:
            if self._mode in ("GDB", "STATUS", "SCROLL"):
                self._switch_to_cgdb()
                event.stop(); return

        # CGDB-mode-only global keys
        if self._mode == "CGDB":
            if key == "i":
                self._switch_to_gdb()
                event.stop(); return
            if key == "s":
                self._switch_to_gdb()
                self.query_one("#gdb-pane", GDBWidget).enter_scroll_mode()
                event.stop(); return
            if key == "colon":
                self.query_one("#status", StatusBar).start_command()
                self._set_mode("STATUS")
                self.query_one("#status", StatusBar).focus()
                event.stop(); return

        # Ctrl-C always interrupts GDB
        if key == "ctrl+c":
            self.gdb.send_interrupt()
            event.stop()

    # ------------------------------------------------------------------
    # Source widget messages
    # ------------------------------------------------------------------

    def on_toggle_breakpoint(self, msg: ToggleBreakpoint) -> None:
        src = self.query_one("#src-pane", SourceView)
        sf  = src.source_file
        if not sf:
            self._show_status("No source file loaded"); return
        existing = next(
            (b for b in self.gdb.breakpoints
             if b.line == msg.line and
             os.path.basename(b.fullname or b.file) == os.path.basename(sf.path)),
            None
        )
        if existing:
            self.gdb.delete_breakpoint(existing.number)
        else:
            self.gdb.set_breakpoint(f"{sf.path}:{msg.line}",
                                    temporary=msg.temporary)

    def on_open_file_dialog(self, msg: OpenFileDialog) -> None:
        # Mirror cgdb interface.cpp: always request fresh source files,
        # then open the dialog when the MI response arrives (_ui_set_source_files).
        # cgdb never shows an error here — it just fires the request and waits.
        self._file_dialog_pending = True
        self.gdb.request_source_files()

    def on_await_mark_jump(self, msg: AwaitMarkJump) -> None:
        self._await_mark_jump = True

    def on_await_mark_set(self, msg: AwaitMarkSet) -> None:
        self._await_mark_set = True

    def on_jump_global_mark(self, msg: JumpGlobalMark) -> None:
        src = self.query_one("#src-pane", SourceView)
        if src.load_file(msg.path):
            src.move_to(msg.line)

    def on_search_start(self, msg: SearchStart) -> None:
        self.query_one("#status", StatusBar).start_search(msg.forward)

    def on_search_update(self, msg: SearchUpdate) -> None:
        self.query_one("#status", StatusBar).update_search(msg.pattern)

    def on_search_commit(self, msg: SearchCommit) -> None:
        self.query_one("#status", StatusBar).cancel_input()
        self._set_mode("CGDB")

    def on_search_cancel(self, msg: SearchCancel) -> None:
        self.query_one("#status", StatusBar).cancel_input()
        self._set_mode("CGDB")

    def on_status_message(self, msg: StatusMessage) -> None:
        self._show_status(msg.text)

    # Quarter-mark split ratios, in increasing src size order.
    # Mirrors cgdb: WIN_SPLIT_GDB_FULL(-2)…WIN_SPLIT_SRC_FULL(2)
    _QUARTER_MARKS = [0.1, 0.25, 0.5, 0.75, 0.9]

    def on_resize_source(self, msg: ResizeSource) -> None:
        is_vertical = (self.cfg.winsplitorientation == "vertical")
        if is_vertical:
            avail = max(4, self.size.width)
        else:
            avail = max(4, self.size.height - 1)
        min_h = self.cfg.winminheight + 1

        if msg.rows:
            # cgdb '=' / '-': change src size by exactly 1 unit
            try:
                src_col = self.query_one("#src-col")
                cur_sz = src_col.size.width if is_vertical else src_col.size.height
            except NoMatches:
                return
            new_sz = max(min_h, min(avail - min_h, cur_sz + msg.delta))
            self._split_ratio = new_sz / avail
            self.cfg.winsplit = "free"
            self._apply_split()

        elif msg.jump:
            # cgdb '+' / '_': snap to next/previous quarter mark
            # (increase_win_height(1) / decrease_win_height(1))
            marks = self._QUARTER_MARKS
            cur   = self._split_ratio
            if msg.delta > 0:
                # find first mark strictly above current ratio
                nxt = next((m for m in marks if m > cur + 0.01), marks[-1])
            else:
                # find last mark strictly below current ratio
                nxt = next((m for m in reversed(marks) if m < cur - 0.01), marks[0])
            self._split_ratio = nxt
            self.cfg.winsplit = "free"   # like cgdb WIN_SPLIT_FREE
            self._apply_split()

        else:
            # legacy percent mode
            self._split_ratio = max(0.1, min(0.9,
                self._split_ratio + msg.delta / 100))
            self._apply_split()

    def on_toggle_orientation(self, _: ToggleOrientation) -> None:
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
        self._set_mode("SCROLL" if msg.active else "GDB")

    def on_scroll_search_start(self, msg: ScrollSearchStart) -> None:
        self.query_one("#status", StatusBar).start_search(msg.forward)

    def on_scroll_search_update(self, msg: ScrollSearchUpdate) -> None:
        self.query_one("#status", StatusBar).update_search(msg.pattern)

    def on_scroll_search_commit(self, msg: ScrollSearchCommit) -> None:
        self.query_one("#status", StatusBar).cancel_input()

    def on_scroll_search_cancel(self, msg: ScrollSearchCancel) -> None:
        self.query_one("#status", StatusBar).cancel_input()

    # ------------------------------------------------------------------
    # Status bar command handling
    # ------------------------------------------------------------------

    def on_command_submit(self, msg: CommandSubmit) -> None:
        err = self.cp.execute(msg.command)
        if err:
            self._show_status(err)
        else:
            self._sync_config()
        self._switch_to_cgdb()

    def on_command_cancel(self, msg: CommandCancel) -> None:
        self._switch_to_cgdb()

    # ------------------------------------------------------------------
    # File dialog
    # ------------------------------------------------------------------

    def on_file_selected(self, msg: FileSelected) -> None:
        self.query_one("#file-dlg", FileDialog).close()
        src = self.query_one("#src-pane", SourceView)
        src.load_file(msg.path)
        self._update_status_file_info()
        self._switch_to_cgdb()

    def on_file_dialog_closed(self, _: FileDialogClosed) -> None:
        self.query_one("#file-dlg", FileDialog).close()
        self._switch_to_cgdb()

    # ------------------------------------------------------------------
    # GDB UI callbacks (scheduled via call_later — runs on main event loop)
    # ------------------------------------------------------------------

    def _ui_on_stopped(self, frame: Frame) -> None:
        """GDB stopped — update source view to executing location."""
        path = frame.fullname or frame.file
        if path and os.path.isfile(path):
            src = self.query_one("#src-pane", SourceView)
            if not src.source_file or src.source_file.path != path:
                src.load_file(path)
            elif self.cfg.autosourcereload:
                src.reload_if_changed()
            src.exe_line = frame.line
            src.move_to(frame.line)
            self._update_status_file_info()
        # Ask GDB for updated breakpoints and source file list
        self.gdb.request_source_files()
        asyncio.ensure_future(self._refresh_breakpoints_async())

    async def _refresh_breakpoints_async(self) -> None:
        await asyncio.sleep(0.15)
        self.gdb.mi_command("-break-list")

    def _ui_on_running(self) -> None:
        self._set_mode("GDB")
        try:
            self.query_one("#gdb-pane").focus()
        except NoMatches:
            pass

    def _ui_set_breakpoints(self, bps: list[Breakpoint]) -> None:
        try:
            self.query_one("#src-pane", SourceView).set_breakpoints(bps)
        except NoMatches:
            pass

    def _ui_set_source_files(self, files: list[str]) -> None:
        try:
            fd = self.query_one("#file-dlg", FileDialog)
            fd.files = files
            # Mirror cgdb update_source_files(): if 'o' was pressed, open
            # the dialog now that the file list has arrived from GDB.
            if self._file_dialog_pending:
                self._file_dialog_pending = False
                if files:
                    fd.open()
                    self._set_mode("FILEDLG")
                else:
                    self._show_status("No sources available! Was the program compiled with debug?")
        except NoMatches:
            pass

    def _ui_load_source_file(self, path: str) -> None:
        """Load a specific source file (from -file-list-exec-source-file)."""
        if not os.path.isfile(path):
            return
        try:
            src = self.query_one("#src-pane", SourceView)
            # Only load if no file is shown yet (don't override a user selection)
            if not src.source_file:
                src.load_file(path)
                self._update_status_file_info()
        except NoMatches:
            pass

    async def _request_initial_source(self) -> None:
        """Wait for GDB's first prompt then query the initial source file."""
        await asyncio.sleep(0.5)
        self.gdb.request_source_file()

    def _ui_gdb_exit(self) -> None:
        # Mirror cgdb: when GDB exits (EOF/error on primary PTY), exit immediately.
        # cgdb calls cgdb_cleanup_and_exit(0) in tgdb_process() on size<=0.
        self.exit(0)

    # ------------------------------------------------------------------
    # Registered commands
    # ------------------------------------------------------------------

    def _register_commands(self) -> None:
        def gdb_cmd(c):
            return lambda a: self._send_gdb_cli(c) or None

        cmds = {
            "quit":    self._cmd_quit,    "q":    self._cmd_quit,
            "help":    self._cmd_help,
            "edit":    self._cmd_edit,    "e":    self._cmd_edit,
            "focus":   self._cmd_focus,
            "insert":  lambda a: self._switch_to_gdb() or None,
            "noh":     self._cmd_noh,
            "shell":   self._cmd_shell,   "sh":   self._cmd_shell,
            "logo":    self._cmd_help,    # show help instead of logo
            "continue": gdb_cmd("continue"), "c":   gdb_cmd("continue"),
            "next":     gdb_cmd("next"),     "n":   gdb_cmd("next"),
            "nexti":    gdb_cmd("nexti"),
            "step":     gdb_cmd("step"),     "s":   gdb_cmd("step"),
            "stepi":    gdb_cmd("stepi"),
            "finish":   gdb_cmd("finish"),   "f":   gdb_cmd("finish"),
            "run":      gdb_cmd("run"),      "r":   gdb_cmd("run"),
            "start":    gdb_cmd("start"),
            "kill":     gdb_cmd("kill"),     "k":   gdb_cmd("kill"),
            "until":    gdb_cmd("until"),    "u":   gdb_cmd("until"),
            "up":       gdb_cmd("up"),
            "down":     gdb_cmd("down"),
        }
        for name, fn in cmds.items():
            self.cp.register_handler(name, fn)

    def _cmd_quit(self, _: list) -> None:
        self.gdb.terminate()
        self.exit(0)

    def _cmd_help(self, _: list) -> None:
        self._show_help_in_source()

    def _cmd_edit(self, _: list) -> None:
        src = self.query_one("#src-pane", SourceView)
        if src.source_file:
            src.source_file._tokens = None
            src.load_file(src.source_file.path)

    def _cmd_focus(self, args: list) -> None:
        if args and args[0].lower() in ("gdb", "gdbw"):
            self._switch_to_gdb()
        else:
            self._switch_to_cgdb()

    def _cmd_noh(self, _: list) -> None:
        self.cfg.hlsearch = False
        try:
            src = self.query_one("#src-pane", SourceView)
            src.hlsearch = False
            src.refresh()
        except NoMatches:
            pass

    def _cmd_shell(self, args: list) -> None:
        import subprocess, shlex
        if args:
            try:
                subprocess.Popen(shlex.join(args), shell=True)
            except Exception as e:
                self._show_status(str(e))

    def _send_gdb_cli(self, cmd: str) -> None:
        self.gdb.send_input(cmd + "\n")
        self._switch_to_gdb()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _apply_split(self) -> None:
        split = self.cfg.winsplit.lower()
        ratio = {"src_full": 0.9, "src_big": 0.7, "even": 0.5,
                 "gdb_big": 0.3, "gdb_full": 0.1}.get(split, self._split_ratio)
        self._split_ratio = ratio
        is_vertical = (self.cfg.winsplitorientation == "vertical")
        try:
            container = self.query_one("#split-container")
            src_col   = self.query_one("#src-col")
            src       = self.query_one("#src-pane")
            gdb       = self.query_one("#gdb-pane")
            status    = self.query_one("#status")
            min_h     = self.cfg.winminheight + 1
            if is_vertical:
                # cgdb WSO_VERTICAL: src+status on left, │ separator, gdb on right
                # src height = screen_rows - 1 (status takes last row of src col)
                # gdb height = screen_rows (full height, no status bar)
                container.styles.layout = "horizontal"
                total_w = max(4, self.size.width)
                src_w   = max(min_h, min(total_w - min_h, int(total_w * ratio)))
                gdb_w   = max(min_h, total_w - src_w)
                # src-col: fixed width, full height (status bar inside it)
                src_col.styles.width  = src_w
                src_col.styles.height = "1fr"
                # src-pane fills src-col minus 1 row for status
                src.styles.width  = "1fr"
                src.styles.height = "1fr"
                # status stays at bottom of src-col (height=1 from CSS)
                status.styles.width = "1fr"
                # gdb: fixed width, full height (no status bar beside it)
                gdb.styles.width  = gdb_w
                gdb.styles.height = "1fr"
            else:
                # cgdb WSO_HORIZONTAL: src on top, status bar below src, gdb below status
                container.styles.layout = "vertical"
                total_h = max(4, self.size.height - 1)
                src_h   = max(min_h, min(total_h - min_h, int(total_h * ratio)))
                gdb_h   = max(min_h, total_h - src_h)
                src_col.styles.width  = "1fr"
                src_col.styles.height = src_h + 1   # src rows + 1 status row
                src.styles.width  = "1fr"
                src.styles.height = src_h
                status.styles.width = "1fr"
                gdb.styles.width  = "1fr"
                gdb.styles.height = gdb_h
        except NoMatches:
            pass

    def on_drag_resize(self, msg: DragResize) -> None:
        """Mouse drag on status bar — resize panes."""
        is_vertical = (self.cfg.winsplitorientation == "vertical")
        min_h = self.cfg.winminheight + 1
        if is_vertical:
            total = self.size.width
            avail = max(4, total)
            src_sz = max(min_h, min(avail - min_h, msg.screen_x if hasattr(msg, 'screen_x') else msg.screen_y))
        else:
            avail = max(4, self.size.height - 1)
            src_sz = max(min_h, min(avail - min_h, msg.screen_y))
        self._split_ratio = src_sz / avail
        self.cfg.winsplit = "free"
        self._apply_split()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_split()
        # GDBWidget.on_resize handles pyte + PTY resize itself via resize_gdb callback

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_status_file_info(self) -> None:
        try:
            src = self.query_one("#src-pane", SourceView)
            if src.source_file:
                self.query_one("#status", StatusBar).set_file_info(
                    src.source_file.path,          # full path, like cgdb source_current_file()
                    src.sel_line,
                    len(src.source_file.lines),
                )
        except NoMatches:
            pass

    def _sync_config(self) -> None:
        cfg = self.cfg
        try:
            src = self.query_one("#src-pane", SourceView)
            src.executing_line_display = cfg.executinglinedisplay
            src.selected_line_display  = cfg.selectedlinedisplay
            src.tabstop    = cfg.tabstop
            src.hlsearch   = cfg.hlsearch
            src.ignorecase = cfg.ignorecase
            src.wrapscan   = cfg.wrapscan
            src.showmarks  = cfg.showmarks
            src.refresh()
        except NoMatches:
            pass
        try:
            gdb_w = self.query_one("#gdb-pane", GDBWidget)
            gdb_w.ignorecase     = cfg.ignorecase
            gdb_w.wrapscan       = cfg.wrapscan
            gdb_w.max_scrollback = cfg.scrollbackbuffersize
        except NoMatches:
            pass
        self.km.timeout_ms       = cfg.timeoutlen
        self.km.ttimeout_ms      = cfg.ttimeoutlen
        self.km.timeout_enabled  = cfg.timeout
        self.km.ttimeout_enabled = cfg.ttimeout
        self._apply_split()

    def _show_help_in_source(self) -> None:
        lines = [
            "tgdb — Python reimplementation of cgdb",
            "",
            "CGDB mode (source window, press ESC):",
            "  j/k      down/up lines           G/gg  bottom/top",
            "  Ctrl-f/b page down/up             H/M/L screen positions",
            "  Ctrl-d/u half page down/up",
            "  /        search forward           ?    search backward",
            "  n/N      next/prev match",
            "  Space    toggle breakpoint        t    temporary breakpoint",
            "  o        open file dialog",
            "  m[a-z]   set local mark           '[a-z]  jump to mark",
            "  ''       last jump location       '.  executing line",
            "  Ctrl-W   toggle split orientation",
            "  -/=      shrink/grow source pane  _/+  by 25%",
            "  F5=run  F6=continue  F7=finish  F8=next  F10=step",
            "  i        switch to GDB mode",
            "  s        switch to GDB scroll mode",
            "  :        command mode",
            "",
            "GDB mode (GDB console, press i):",
            "  ESC      back to CGDB mode        PageUp  scroll mode",
            "  All keys forwarded to GDB (readline, history, etc.)",
            "",
            "Scroll mode (PageUp in GDB window):",
            "  j/k/PageUp/Dn  scroll             G/gg  end/beginning",
            "  //?/n/N  search                   q/i/Enter  exit scroll",
            "",
            "Commands (type : in CGDB mode):",
            "  :set tabstop=4          :set hlsearch",
            "  :set winsplit=even      :set executinglinedisplay=longarrow",
            "  :highlight Statement ctermfg=Yellow cterm=bold",
            "  :map <F8> :next<Enter>  :imap <F8> :next<Enter>",
            "  :break :continue :next :step :finish :run :quit",
        ]
        sf = SourceFile("<help>", lines)
        src = self.query_one("#src-pane", SourceView)
        src.source_file = sf
        src.exe_line = 0
        src.move_to(1)
        self._switch_to_cgdb()
