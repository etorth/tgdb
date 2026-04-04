"""
Main Textual application — mirrors cgdb's interface.cpp + cgdb.cpp.

Global layout:
  ┌──────────────────────────┐
  │    Source / GDB area     │
  ├──────────────────────────┤
  │    command-line bar      │  1 line
  └──────────────────────────┘

The source pane itself reserves its bottom row for the current file path.

Modes: TGDB | GDB | SCROLL | CMD | MESSAGE | FILEDLG
"""

from __future__ import annotations

import asyncio
from typing import Optional

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.css.query import NoMatches

from .highlight_groups import HighlightGroups
from .key_mapper import KeyMapper
from .config import Config, ConfigParser
from .gdb_controller import (
    GDBController,
    Frame,
    LocalVariable,
    RegisterInfo,
    ThreadInfo,
)
from .source_widget import (
    SourceView,
)
from .gdb_widget import (
    GDBWidget,
)
from .command_line_bar import CommandLineBar
from .file_dialog import FileDialog
from .context_menu import ContextMenu
from .local_variable_pane import LocalVariablePane
from .register_pane import RegisterPane
from .stack_pane import StackPane
from .thread_pane import ThreadPane
from .workspace import PaneContainer, PaneDescriptor
from .xdg_path import XDGPath
from .app_commands import CommandsMixin
from .app_workspace import WorkspaceMixin
from .app_layout import LayoutMixin
from .app_keys import KeyRoutingMixin
from .app_callbacks import CallbacksMixin


class TGDBApp(CommandsMixin, WorkspaceMixin, LayoutMixin, KeyRoutingMixin, CallbacksMixin, App):
    """tgdb — Python front-end for GDB, compatible with cgdb."""

    CSS = """
    Screen {
        layers: base dialog;
        layout: vertical;
    }
    #global-container {
        layer: base;
        layout: vertical;
        height: 1fr;
        width: 1fr;
    }
    #split-container {
        height: 1fr;
        width: 1fr;
    }
    #src-pane {
        width: 1fr;
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #cmdline {
        height: 1;
        width: 1fr;
    }
    #gdb-pane {
        width: 1fr;
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #context-menu {
        display: none;
    }
    #context-menu.visible {
        display: block;
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

    def __init__(self, gdb_path: str = "gdb", gdb_args: list[str] | None = None, rc_file: Optional[str] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = HighlightGroups()
        self.km = KeyMapper()
        self.cfg = Config()
        self.cp = ConfigParser(self.cfg, self.hl, self.km)
        self._initial_source_pending = bool(gdb_args)
        self._register_commands()

        # Wire the tgdb stdlib singleton to this app instance.
        # This makes ``import tgdb; tgdb.screen.split(...)`` work inside
        # :python blocks without exposing any internal tgdb classes.
        import tgdb as _tgdb_pkg

        _tgdb_pkg.screen._set_app(self)
        self.cp.set_py_globals({"app": self, "tgdb": _tgdb_pkg})

        self._rc_file: Optional[str] = rc_file  # resolved in on_mount after app is ready

        self.gdb = GDBController(gdb_path=gdb_path, args=gdb_args or [])
        self._gdb_task: Optional[asyncio.Task] = None
        self._cmd_task: Optional[asyncio.Task] = None  # running CommandLineBar command
        self._pending_replay_tokens: list[str] = []  # map tokens queued after async <CR>

        self._mode: str = "GDB"
        self._await_mark_jump: bool = False
        self._await_mark_set: bool = False
        self._split_ratio: float = 0.5
        self._cur_win_split: int = {
            "gdb_full": -2,
            "gdb_big": -1,
            "even": 0,
            "src_big": 1,
            "src_full": 2,
        }.get(self.cfg.winsplit.lower(), 0)
        self._window_shift: int = 0
        self._last_split_setting: str = ""
        self._last_orientation: str = ""
        self._preserve_window_shift_once: bool = False
        self._file_dialog_pending: bool = False
        self._inf_tty_fd: Optional[int] = None
        self._context_menu_target: Optional[Widget] = None
        self._source_view: Optional[SourceView] = None
        self._gdb_widget: Optional[GDBWidget] = None
        self._locals_pane: Optional[LocalVariablePane] = None
        self._current_locals: list[LocalVariable] = []
        self._stack_pane: Optional[StackPane] = None
        self._current_stack: list[Frame] = []
        self._thread_pane: Optional[ThreadPane] = None
        self._current_threads: list[ThreadInfo] = []
        self._register_pane: Optional[RegisterPane] = None
        self._current_registers: list[RegisterInfo] = []
        self._in_map_replay: bool = False
        self._pane_descriptors: dict[str, PaneDescriptor] = {
            "source": PaneDescriptor("Source", self._make_source_pane, lambda: self._source_view),
            "gdb": PaneDescriptor("GDB", self._make_gdb_pane, lambda: self._gdb_widget),
            "locals": PaneDescriptor(
                "Local Variables",
                self._make_local_variable_pane,
                lambda: self._locals_pane,
                lambda: self.gdb.request_current_frame_locals(report_error=False),
            ),
            "registers": PaneDescriptor(
                "Registers",
                self._make_register_pane,
                lambda: self._register_pane,
                lambda: self.gdb.request_current_registers(report_error=False),
            ),
            "stack": PaneDescriptor(
                "Stack",
                self._make_stack_pane,
                lambda: self._stack_pane,
                lambda: self.gdb.request_current_stack_frames(report_error=False),
            ),
            "threads": PaneDescriptor(
                "Threads",
                self._make_thread_pane,
                lambda: self._thread_pane,
                lambda: self.gdb.request_current_threads(report_error=False),
            ),
        }
        self._add_menu_order: tuple[str, ...] = (
            "source",
            "gdb",
            "locals",
            "registers",
            "threads",
            "stack",
        )

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        if self._source_view is None:
            self._source_view = SourceView(self.hl, id="src-pane")
        if self._gdb_widget is None:
            self._gdb_widget = GDBWidget(
                self.hl,
                max_scrollback=self.cfg.scrollbackbuffersize,
                id="gdb-pane",
            )
        with Widget(id="global-container"):
            yield PaneContainer(
                self.hl,
                orientation=self.cfg.winsplitorientation,
                id="split-container",
            )
            yield CommandLineBar(self.hl, completion_provider=self.cp.get_completions, id="cmdline")
        yield FileDialog(self.hl, id="file-dlg")
        yield ContextMenu(self.hl, id="context-menu")

    # ------------------------------------------------------------------
    # on_mount — async so asyncio.create_task works
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        # Configure source widget
        src = self._get_source_view()
        gdb_w = self._get_gdb_widget()
        if src is None or gdb_w is None:
            self._show_status("Failed to initialize core panes")
            return
        src.executing_line_display = self.cfg.executinglinedisplay
        src.selected_line_display = self.cfg.selectedlinedisplay
        src.tabstop = self.cfg.tabstop
        src.hlsearch = self.cfg.hlsearch
        src.ignorecase = self.cfg.ignorecase
        src.wrapscan = self.cfg.wrapscan
        src.showmarks = self.cfg.showmarks

        # Configure GDB widget
        gdb_w.ignorecase = self.cfg.ignorecase
        gdb_w.wrapscan = self.cfg.wrapscan
        gdb_w.send_to_gdb = self.gdb.send_input  # bytes → primary PTY
        gdb_w.resize_gdb = self.gdb.resize  # keep pyte in sync
        gdb_w.on_switch_to_tgdb = self._switch_to_tgdb
        gdb_w.imap_feed = self._imap_feed
        gdb_w.imap_replay = self._replay_gdb_key_sequence

        # Mount src and gdb into the root PaneContainer (always present).
        try:
            container = self.query_one("#split-container", PaneContainer)
            await container.set_items([src, gdb_w])
        except Exception as exc:
            self._show_status(f"Failed to initialize workspace: {exc}")
            return

        # Configure file dialog
        fd = self.query_one("#file-dlg", FileDialog)
        fd.ignorecase = self.cfg.ignorecase
        fd.wrapscan = self.cfg.wrapscan

        # Configure command-line bar — history
        try:
            cmdline = self.query_one("#cmdline", CommandLineBar)
            cmdline._history_file = XDGPath.state_home() / "tgdb" / "history"
            self.cp.set_cmdline_bar(cmdline)
            cmdline.load_history()
            # Session delimiter — recorded in history so sessions are visible when browsing
            from datetime import datetime

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cmdline._add_to_history(f"# tgdb begins {ts}", max_size=self.cfg.historysize)
        except NoMatches:
            pass

        # GDB callbacks — on_console now delivers raw bytes from GDB's PTY,
        # fed directly into the pyte VT100 emulator (matching cgdb's libvterm).
        self.gdb.on_console = lambda data: self.call_later(gdb_w.feed_bytes, data)
        self.gdb.on_stopped = lambda f: self.call_later(self._ui_on_stopped, f)
        self.gdb.on_running = lambda: self.call_later(self._ui_on_running)
        self.gdb.on_breakpoints = lambda b: self.call_later(self._ui_set_breakpoints, b)
        self.gdb.on_source_files = lambda f: self.call_later(self._ui_set_source_files, f)
        self.gdb.on_source_file = lambda f, ln: self.call_later(self._ui_load_source_file, f, ln)
        self.gdb.on_locals = lambda v: self.call_later(self._ui_set_locals, v)
        self.gdb.on_registers = lambda v: self.call_later(self._ui_set_registers, v)
        self.gdb.on_stack = lambda v: self.call_later(self._ui_set_stack, v)
        self.gdb.on_threads = lambda v: self.call_later(self._ui_set_threads, v)
        self.gdb.on_exit = lambda: self.call_later(self._ui_gdb_exit)
        self.gdb.on_error = lambda m: self.call_later(self._show_status, f"Error: {m}")

        # Start GDB process
        try:
            self.gdb.start(rows=40, cols=200)
        except Exception as e:
            self._show_status(f"Failed to start GDB: {e}")
            return

        # Start async read loop
        self._gdb_task = asyncio.create_task(self.gdb.run_async())
        asyncio.create_task(self._request_initial_location())

        # Initial mode: GDB focused.
        self._set_mode("GDB")
        gdb_w.focus()

        # Source the rc file before handing control to the user.
        # This keeps startup semantics "as if" the user sourced the file
        # immediately before the interactive session begins, instead of letting
        # a background task race with real user input.
        await self._load_rc_async()

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    def _save_history_to_disk(self) -> None:
        """Persist command history to the history file (best-effort)."""
        try:
            bar = self.query_one("#cmdline", CommandLineBar)
            bar.save_history(max_size=self.cfg.historysize)
        except Exception:
            pass

    async def on_unmount(self) -> None:
        """Save command history and cancel background tasks when tgdb exits."""
        self._save_history_to_disk()
        if self._gdb_task and not self._gdb_task.done():
            self._gdb_task.cancel()
            try:
                await self._gdb_task
            except asyncio.CancelledError:
                pass

    async def _load_rc_async(self) -> None:
        """Source the rc file as if the user had typed each command interactively.

        ``--rcfile NONE`` skips loading entirely.  Otherwise the default rc is
        ``~/.config/tgdb/tgdbrc`` (or the path given with ``--rcfile``).
        """
        if self._rc_file == "NONE":
            return
        if self._rc_file:
            path: Optional[str] = self._rc_file
        else:
            p = self.cp.default_rc_path()
            if p is None:
                return
            path = str(p)
        err = await self.cp.load_file_async(path)
        if err:
            self._show_status(err)
        self._sync_config()

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        try:
            status = self.query_one("#cmdline", CommandLineBar)
            status.set_mode(mode)
            self._update_status_file_info()
        except NoMatches:
            pass
        # cgdb: scr_refresh(gdb_scroller, focus==GDB, ...) — hide GDB cursor when not focused
        gdb_w = self._get_gdb_widget()
        if gdb_w is not None:
            gdb_w.gdb_focused = mode in ("GDB", "SCROLL")
            gdb_w.refresh()

    def action_help_quit(self) -> None:
        """Suppress Textual's default Ctrl+C 'press Ctrl+Q to quit' notification.

        Textual 8.x binds Ctrl+C to this action (priority=False) and calls it
        via App._on_key after forwarding the key to the focused widget.
        In tgdb Ctrl+C is always an interrupt signal for GDB — never a quit
        prompt — so we override this to a no-op.
        """

    def _switch_to_tgdb(self) -> None:
        self._set_mode("TGDB")
        if self._focus_widget(self._get_source_view(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())

    def _switch_to_gdb(self) -> None:
        self._set_mode("GDB")
        if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())

    def _enter_cmd_mode(self) -> bool:
        """Activate the command-line bar (CMD mode).  Returns True on success."""
        try:
            bar = self.query_one("#cmdline", CommandLineBar)
            bar.start_command()
            self._set_mode("CMD")
            bar.focus()
            return True
        except NoMatches:
            return False

    def _show_status(self, msg: str) -> bool:
        """Show *msg* in the status bar.

        If the message spans multiple lines the bar expands and the app enters
        MESSAGE mode (waiting for user to dismiss).  Returns True in that case
        so the caller knows not to switch focus away.
        """
        try:
            status = self.query_one("#cmdline", CommandLineBar)
            if "\n" in msg:
                status.show_multiline_message(msg)
                self._set_mode("MESSAGE")
                status.focus()
                return True
            else:
                status.show_message(msg)
                return False
        except NoMatches:
            return False

    @staticmethod
    def _widget_attached(widget: Optional[Widget]) -> bool:
        return widget is not None and widget.parent is not None

    def _get_source_view(self, *, mounted_only: bool = False) -> Optional[SourceView]:
        if self._source_view is None:
            return None
        if mounted_only and not self._widget_attached(self._source_view):
            return None
        return self._source_view

    def _get_gdb_widget(self, *, mounted_only: bool = False) -> Optional[GDBWidget]:
        if self._gdb_widget is None:
            return None
        if mounted_only and not self._widget_attached(self._gdb_widget):
            return None
        return self._gdb_widget

    def _focus_widget(self, widget: Optional[Widget]) -> bool:
        if not self._widget_attached(widget):
            return False
        try:
            widget.focus()
        except Exception:
            return False
        return True

    def _first_workspace_leaf(self, widget: Optional[Widget] = None) -> Optional[Widget]:
        if widget is None:
            try:
                widget = self.query_one("#split-container", PaneContainer)
            except NoMatches:
                return None

        if isinstance(widget, PaneContainer):
            for item in widget.items:
                leaf = self._first_workspace_leaf(item)
                if leaf is not None:
                    return leaf
            return None

        if getattr(widget, "can_focus", False) and self._widget_attached(widget):
            return widget
        return None

