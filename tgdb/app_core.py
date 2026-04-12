"""Core lifecycle and focus helpers for ``TGDBApp``."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.widget import Widget

from .command_line_bar import CommandLineBar
from .context_menu import ContextMenu
from .file_dialog import FileDialog
from .gdb_widget import GDBWidget
from .source_widget import SourceView
from .workspace import PaneContainer
from .xdg_path import XDGPath


class AppCoreMixin:
    """Mixin providing app composition, lifecycle, and focus helpers."""

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
            yield CommandLineBar(
                self.hl,
                completion_provider=self.cp.get_completions,
                id="cmdline",
            )
        yield FileDialog(self.hl, id="file-dlg")
        yield ContextMenu(self.hl, id="context-menu")


    async def on_mount(self) -> None:
        source_view = self._get_source_view()
        gdb_widget = self._get_gdb_widget()
        if source_view is None or gdb_widget is None:
            self._show_status("Failed to initialize core panes")
            return
        source_view.executing_line_display = self.cfg.executinglinedisplay
        source_view.selected_line_display = self.cfg.selectedlinedisplay
        source_view.tabstop = self.cfg.tabstop
        source_view.hlsearch = self.cfg.hlsearch
        source_view.ignorecase = self.cfg.ignorecase
        source_view.wrapscan = self.cfg.wrapscan
        source_view.showmarks = self.cfg.showmarks

        gdb_widget.ignorecase = self.cfg.ignorecase
        gdb_widget.wrapscan = self.cfg.wrapscan
        gdb_widget.send_to_gdb = self.gdb.send_input
        gdb_widget.resize_gdb = self.gdb.resize
        gdb_widget.on_switch_to_tgdb = self._switch_to_tgdb
        gdb_widget.imap_feed = self._imap_feed
        gdb_widget.imap_replay = self._replay_gdb_key_sequence

        try:
            container = self.query_one("#split-container", PaneContainer)
            await container.set_items([source_view, gdb_widget])
        except Exception as exc:
            self._show_status(f"Failed to initialize workspace: {exc}")
            return

        file_dialog = self.query_one("#file-dlg", FileDialog)
        file_dialog.ignorecase = self.cfg.ignorecase
        file_dialog.wrapscan = self.cfg.wrapscan

        try:
            cmdline = self.query_one("#cmdline", CommandLineBar)
            cmdline._history_file = XDGPath.state_home() / "tgdb" / "history"
            self.cp.set_cmdline_bar(cmdline)
            cmdline.load_history()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cmdline._add_to_history(
                f"# tgdb begins {timestamp}",
                max_size=self.cfg.historysize,
            )
        except NoMatches:
            pass

        self.gdb.on_console = lambda data: self.call_later(gdb_widget.feed_bytes, data)
        self.gdb.on_stopped = lambda frame: self.call_later(self._ui_on_stopped, frame)
        self.gdb.on_running = lambda: self.call_later(self._ui_on_running)
        self.gdb.on_breakpoints = lambda breakpoints: self.call_later(
            self._ui_set_breakpoints,
            breakpoints,
        )
        self.gdb.on_source_files = lambda files: self.call_later(
            self._ui_set_source_files,
            files,
        )
        self.gdb.on_source_file = lambda path, line: self.call_later(
            self._ui_load_source_file,
            path,
            line,
        )
        self.gdb.on_locals = lambda variables: self.call_later(self._ui_set_locals, variables)
        self.gdb.on_registers = lambda registers: self.call_later(
            self._ui_set_registers,
            registers,
        )
        self.gdb.on_stack = lambda frames: self.call_later(self._ui_set_stack, frames)
        self.gdb.on_threads = lambda threads: self.call_later(self._ui_set_threads, threads)
        self.gdb.on_exit = lambda: self.call_later(self._ui_gdb_exit)
        self.gdb.on_error = lambda msg: self.call_later(self._show_status, f"Error: {msg}")

        try:
            self.gdb.start(rows=40, cols=200)
        except Exception as exc:
            self._show_status(f"Failed to start GDB: {exc}")
            return

        self._gdb_task = asyncio.create_task(self.gdb.run_async())
        asyncio.create_task(self._request_initial_location())

        self._set_mode("GDB_PROMPT")
        gdb_widget.focus()

        await self._load_rc_async()


    def _save_history_to_disk(self) -> None:
        try:
            bar = self.query_one("#cmdline", CommandLineBar)
            bar.save_history(max_size=self.cfg.historysize)
        except Exception:
            pass


    async def on_unmount(self) -> None:
        self._save_history_to_disk()
        if self._gdb_task and not self._gdb_task.done():
            self._gdb_task.cancel()
            try:
                await self._gdb_task
            except asyncio.CancelledError:
                pass


    async def _load_rc_async(self) -> None:
        if self._rc_file == "NONE":
            return
        path: Optional[str]
        if self._rc_file:
            path = self._rc_file
        else:
            default_path = self.cp.default_rc_path()
            if default_path is None:
                return
            path = str(default_path)
        error = await self.cp.load_file_async(path)
        if error:
            self._show_status(error)
        self._sync_config()


    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        try:
            status = self.query_one("#cmdline", CommandLineBar)
            status.set_mode(mode)
            self._update_status_file_info()
        except NoMatches:
            pass
        gdb_widget = self._get_gdb_widget()
        if gdb_widget is not None:
            gdb_widget.gdb_focused = mode in ("GDB_PROMPT", "GDB_SCROLL")
            gdb_widget.refresh()


    def action_help_quit(self) -> None:
        """Suppress Textual's default Ctrl+C quit notification."""


    def _switch_to_tgdb(self) -> None:
        self._set_mode("TGDB")
        if self._focus_widget(self._get_source_view(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())


    def _switch_to_gdb(self) -> None:
        self._set_mode("GDB_PROMPT")
        if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())


    def _enter_cmd_mode(self) -> bool:
        try:
            bar = self.query_one("#cmdline", CommandLineBar)
            bar.start_command()
            self._set_mode("CMD")
            bar.focus()
            return True
        except NoMatches:
            return False


    def _show_status(self, msg: str) -> bool:
        try:
            status = self.query_one("#cmdline", CommandLineBar)
            if "\n" in msg:
                status.show_multiline_message(msg)
                self._set_mode("ML_MESSAGE")
                status.focus()
                return True
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
