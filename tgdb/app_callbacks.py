"""CallbacksMixin — message handlers and GDB UI callbacks extracted from TGDBApp."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from textual.css.query import NoMatches

from .source_widget import (
    ToggleBreakpoint,
    OpenFileDialog,
    OpenTTY,
    AwaitMarkJump,
    AwaitMarkSet,
    JumpGlobalMark,
    SearchStart,
    SearchUpdate,
    SearchCommit,
    SearchCancel,
    StatusMessage,
    GDBCommand,
    ShowHelp,
)
from .gdb_widget import (
    ScrollModeChange,
    ScrollSearchStart,
    ScrollSearchUpdate,
    ScrollSearchCommit,
    ScrollSearchCancel,
)
from .command_line_bar import (
    CommandLineBar,
    CommandSubmit,
    CommandCancel,
    MessageDismissed,
)
from .file_dialog import FileDialog, FileSelected, FileDialogClosed
from .gdb_controller import Breakpoint, Frame, LocalVariable, ThreadInfo, RegisterInfo

_log = logging.getLogger("tgdb.app")


class CallbacksMixin:
    """Message handlers and GDB UI callbacks for TGDBApp."""

    def on_toggle_breakpoint(self, msg: ToggleBreakpoint) -> None:
        src = self._get_source_view()
        if src is None:
            self._show_status("No source pane available")
            return
        sf = src.source_file
        if not sf:
            self._show_status("No source file loaded")
            return
        existing = None
        target_basename = os.path.basename(sf.path)
        for b in self.gdb.breakpoints:
            b_basename = os.path.basename(b.fullname or b.file)
            if b.line == msg.line and b_basename == target_basename:
                existing = b
                break
        _log.info(f"toggle breakpoint line={msg.line} file={sf.path}")
        if existing:
            self.gdb.delete_breakpoint(existing.number)
        else:
            self.gdb.set_breakpoint(f"{sf.path}:{msg.line}", temporary=msg.temporary)


    def on_open_file_dialog(self, msg: OpenFileDialog) -> None:
        # Open immediately and show a pending state while GDB enumerates files.
        # Large binaries can make -file-list-exec-source-files noticeably slow.
        try:
            fd = self.query_one("#file-dlg", FileDialog)
        except NoMatches:
            self._show_status("File dialog is unavailable")
            return
        self._file_dialog_pending = True
        fd.open_pending()
        self._set_mode("FILEDLG")
        self.gdb.request_source_files()


    def on_open_tty(self, _: OpenTTY) -> None:
        """Ctrl-T: allocate a new PTY for the inferior's stdio.
        Mirrors cgdb: open a PTY pair and tell GDB 'set inferior-tty <slave>'."""
        try:
            master_fd, slave_fd = os.openpty()
            slave_path = os.ttyname(slave_fd)
            os.close(slave_fd)
            # Store master_fd so it stays open (inferior can write to slave)
            if self._inf_tty_fd is not None:
                try:
                    os.close(self._inf_tty_fd)
                except OSError:
                    pass
            self._inf_tty_fd = master_fd
            self.gdb.send_input(f"set inferior-tty {slave_path}\n")
            self._show_status(f"Inferior TTY: {slave_path}")
        except OSError as e:
            self._show_status(f"TTY error: {e}")


    def on_await_mark_jump(self, msg: AwaitMarkJump) -> None:
        self._await_mark_jump = True


    def on_await_mark_set(self, msg: AwaitMarkSet) -> None:
        self._await_mark_set = True


    def on_jump_global_mark(self, msg: JumpGlobalMark) -> None:
        src = self._get_source_view()
        if src is not None and src.load_file(msg.path):
            src.move_to(msg.line)


    def on_search_start(self, msg: SearchStart) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).start_search(msg.forward)
        except NoMatches:
            pass


    def on_search_update(self, msg: SearchUpdate) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).update_search(msg.pattern)
        except NoMatches:
            pass


    def on_search_commit(self, msg: SearchCommit) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).cancel_input()
        except NoMatches:
            pass
        self._set_mode("TGDB")


    def on_search_cancel(self, msg: SearchCancel) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).cancel_input()
        except NoMatches:
            pass
        self._set_mode("TGDB")


    def on_status_message(self, msg: StatusMessage) -> None:
        self._show_status(msg.text)


    def on_gdb_command(self, msg: GDBCommand) -> None:
        self._send_gdb_cli(msg.cmd)


    def on_show_help(self, _: ShowHelp) -> None:
        self._show_help_in_source()


    def on_scroll_mode_change(self, msg: ScrollModeChange) -> None:
        if msg.active:
            mode = "GDB_SCROLL"
        else:
            mode = "GDB_PROMPT"
        self._set_mode(mode)


    def on_scroll_search_start(self, msg: ScrollSearchStart) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).start_search(msg.forward)
        except NoMatches:
            pass


    def on_scroll_search_update(self, msg: ScrollSearchUpdate) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).update_search(msg.pattern)
        except NoMatches:
            pass


    def on_scroll_search_commit(self, msg: ScrollSearchCommit) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).cancel_input()
        except NoMatches:
            pass
        self._set_mode("GDB_SCROLL")


    def on_scroll_search_cancel(self, msg: ScrollSearchCancel) -> None:
        try:
            self.query_one("#cmdline", CommandLineBar).cancel_input()
        except NoMatches:
            pass
        self._set_mode("GDB_SCROLL")


    def on_command_submit(self, msg: CommandSubmit) -> None:
        _log.info(f"command: {msg.command!r}")
        # If a command task is already running, reject new submissions.
        if self._cmd_task is not None and not self._cmd_task.done():
            self._show_status("Command still running (Ctrl+C to cancel)")
            return
        self._cmd_task = asyncio.create_task(
            self._run_cmd_task(msg.command, history_text=msg.history_text)
        )


    async def _run_cmd_task(self, cmd: str, *, history_text: str = "") -> None:
        """Run one CommandLineBar command as an async task (one-at-a-time)."""
        try:
            cmdline = self.query_one("#cmdline", CommandLineBar)
        except NoMatches:
            return

        # Record in history before execution.
        # Use verbatim history_text if provided (heredoc), otherwise cmd itself.
        hist = (history_text or cmd).strip()
        if hist:
            cmdline._add_to_history(hist, max_size=self.cfg.historysize)

        task_lock_gen = cmdline.lock_for_task()
        error_output: Optional[str] = None
        cancelled = False

        def _print_fn(chunk: str) -> None:
            cmdline.append_output(chunk, task_gen=task_lock_gen)

        try:
            result = await self.cp.execute_async(cmd, print_fn=_print_fn)
            if result:
                error_output = result
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            error_output = f"Internal error: {exc}"

        # Retrieve all lines collected during task execution (sync-print-op)
        collected = cmdline.get_collected_output()
        cmdline.finish_task()
        self._cmd_task = None

        if cancelled:
            text = (
                ("\n".join(collected) + "\n[Interrupted]")
                if collected
                else "[Interrupted]"
            )
            error_output = text.strip()
            collected = []  # already merged into error_output

        # Build sync-print-op output
        if error_output:
            # Errors always shown as message
            if collected:
                captured = "\n".join(collected) + "\n" + error_output
            else:
                captured = error_output
            if self._show_status(captured):
                self._resume_pending_replay()
                return  # ML_MESSAGE mode — stay until user dismisses
        elif collected:
            if len(collected) == 1:
                # Single line: show briefly, no "Press ENTER" prompt
                self._show_status(collected[0])
            else:
                # Multi-line: show with "Press ENTER" prompt
                captured = "\n".join(collected)
                if self._show_status(captured):
                    self._resume_pending_replay()
                    return  # ML_MESSAGE mode
        else:
            self._sync_config()

        if self._mode == "CMD":
            self._switch_to_tgdb()
        self._resume_pending_replay()


    def on_command_cancel(self, msg: CommandCancel) -> None:
        self._switch_to_tgdb()


    def on_message_dismissed(self, msg: MessageDismissed) -> None:
        # Only act if still in ML_MESSAGE mode.  During map replay the synchronous
        # mode switch in _dispatch_key_internal already moved us out of ML_MESSAGE;
        # this handler becomes a no-op so it can't kill a subsequent CMD entry.
        if self._mode == "ML_MESSAGE":
            self._switch_to_tgdb()


    def on_file_selected(self, msg: FileSelected) -> None:
        self._file_dialog_pending = False
        self.query_one("#file-dlg", FileDialog).close()
        src = self._get_source_view()
        if src is not None:
            src.load_file(msg.path)
            self._update_status_file_info()
        self._switch_to_tgdb()


    def on_file_dialog_closed(self, _: FileDialogClosed) -> None:
        self._file_dialog_pending = False
        self.query_one("#file-dlg", FileDialog).close()
        self._switch_to_tgdb()


    def _ui_on_stopped(self, frame: Frame) -> None:
        """GDB stopped — update source view to executing location."""
        path = frame.file or frame.fullname
        _log.info(f"stopped frame={path}:{frame.line} func={frame.func}")
        path = frame.fullname or frame.file
        if path and os.path.isfile(path):
            src = self._get_source_view()
            if src is not None:
                if not src.source_file or src.source_file.path != path:
                    src.load_file(path)
                elif self.cfg.autosourcereload:
                    src.reload_if_changed()
                src.exe_line = frame.line
                src.move_to(frame.line)
                self._update_status_file_info()
        # Refresh the full source-file list only when the file dialog is waiting
        # for it; on large binaries this MI query can block interactive GDB input
        # long enough to make the console look hung after commands like `start`.
        if self._file_dialog_pending:
            self.gdb.request_source_files()
        asyncio.create_task(self._refresh_breakpoints_async())
        # Refresh watch and disasm panes when stopped
        if self._evaluate_pane is not None:
            asyncio.create_task(self._evaluate_pane.refresh_all())
        if self._disasm_pane is not None:
            asyncio.create_task(
                self._disasm_pane.refresh_disasm(
                    path or "", frame.line
                )
            )


    async def _refresh_breakpoints_async(self) -> None:
        await asyncio.sleep(0.15)
        self.gdb.mi_command("-break-list")


    def _ui_on_running(self) -> None:
        _log.info("running")
        self._set_mode("GDB_PROMPT")
        if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())


    def _ui_set_breakpoints(self, bps: list[Breakpoint]) -> None:
        _log.info(f"breakpoints: {len(bps)}")
        src = self._get_source_view()
        if src is not None:
            src.set_breakpoints(bps)


    def _ui_set_locals(self, variables: list[LocalVariable]) -> None:
        _log.debug(f"locals: {len(variables)} vars")
        self._current_locals = list(variables)
        if self._locals_pane is not None:
            self._locals_pane.set_variables(
                self._current_locals, self.gdb.current_frame
            )


    def _ui_set_registers(self, registers: list[RegisterInfo]) -> None:
        self._current_registers = list(registers)
        if self._register_pane is not None:
            self._register_pane.set_registers(self._current_registers)


    def _ui_set_stack(self, frames: list[Frame]) -> None:
        self._current_stack = list(frames)
        if self.gdb.current_frame:
            current_level = self.gdb.current_frame.level
        else:
            current_level = 0
        if self._stack_pane is not None:
            self._stack_pane.set_frames(
                self._current_stack, current_level=current_level
            )


    def _ui_set_threads(self, threads: list[ThreadInfo]) -> None:
        self._current_threads = list(threads)
        if self._thread_pane is not None:
            self._thread_pane.set_threads(self._current_threads)


    def _ui_set_source_files(self, files: list[str]) -> None:
        try:
            fd = self.query_one("#file-dlg", FileDialog)
        except NoMatches:
            self._file_dialog_pending = False
            return
        pending = self._file_dialog_pending
        self._file_dialog_pending = False
        if not pending or not fd.is_open:
            return
        fd.files = files


    def _ui_load_source_file(self, path: str, line: int = 0) -> None:
        """Load a specific source file (from -file-list-exec-source-file)."""
        if not os.path.isfile(path):
            return
        src = self._get_source_view()
        if src is None:
            return
        # Only load if no file is shown yet (don't override a user selection)
        if not src.source_file:
            _log.info(f"load source {path} line {line}")
            src.load_file(path)
            if line > 0:
                src.move_to(line)
            self._initial_source_pending = False
            src.run_pending_search()
            self._update_status_file_info()


    async def _request_initial_location(self) -> None:
        """Mirror cgdb startup: query current location without surfacing noise."""
        await asyncio.sleep(0.5)
        self.gdb.request_current_location(report_error=False)


    def _ui_gdb_exit(self) -> None:
        # Mirror cgdb: when GDB exits (EOF/error on primary PTY), exit immediately.
        # cgdb calls cgdb_cleanup_and_exit(0) in tgdb_process() on size<=0.
        _log.info("GDB exited")
        self._save_history_to_disk()
        self.exit(0)


    def _update_status_file_info(self) -> None:
        src = self._get_source_view()
        if src is not None:
            src.refresh()


    def _sync_config(self) -> None:
        cfg = self.cfg
        src = self._get_source_view()
        if src is not None:
            old_tabstop = src.tabstop
            src.executing_line_display = cfg.executinglinedisplay
            src.selected_line_display = cfg.selectedlinedisplay
            src.tabstop = cfg.tabstop
            src.hlsearch = cfg.hlsearch
            src.ignorecase = cfg.ignorecase
            src.wrapscan = cfg.wrapscan
            src.showmarks = cfg.showmarks
            src.color = cfg.color
            # If tabstop changed, reload the current file so tabs are re-expanded
            # and the token cache is rebuilt with the new width.
            if cfg.tabstop != old_tabstop and src.source_file and src.source_file.path:
                src.load_file(src.source_file.path)
            else:
                src.refresh()
        gdb_w = self._get_gdb_widget()
        if gdb_w is not None:
            gdb_w.ignorecase = cfg.ignorecase
            gdb_w.wrapscan = cfg.wrapscan
            gdb_w.max_scrollback = cfg.scrollbackbuffersize
            gdb_w.debugwincolor = cfg.debugwincolor
            if gdb_w._screen:
                gdb_w._screen.use_color = cfg.debugwincolor
            gdb_w.refresh()
        self.km.timeout_ms = cfg.timeoutlen
        self.km.ttimeout_ms = cfg.ttimeoutlen
        self.km.timeout_enabled = cfg.timeout
        self.km.ttimeout_enabled = cfg.ttimeout
        if cfg.winsplitorientation != self._last_orientation:
            self._set_window_shift_from_ratio(
                cfg.winsplitorientation == "horizontal",
                self._split_ratio,
            )
            self._preserve_window_shift_once = True
        self._apply_split()
