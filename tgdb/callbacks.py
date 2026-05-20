"""Message handlers and GDB UI callbacks for the application package."""

import asyncio
import logging
import os

from textual.css.query import NoMatches

from .async_util import _on_task_done
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
    CompletionPopup,
    CompletionPopupHide,
    CompletionPopupShow,
    CompletionPopupUpdate,
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
            # ``existing.number`` may be a child id like ``"3.1"`` (one
            # location of a multi-location breakpoint).  GDB only deletes
            # by parent number, and deleting the parent removes every
            # location, which is the user-visible expectation here.
            parent_number = existing.number.partition(".")[0]
            self.gdb.delete_breakpoint(parent_number)
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


    def _close_shared_search(self, *, set_mode: str, clear_other: str) -> None:
        """Finalise the shared cmdline-bar search and switch mode.

        The cmdline bar is shared between source-pane search (``/``)
        and gdb-scroll search; both widgets track their own
        ``_search_active`` flag.  A sequence like "press ``/`` in
        source, focus jumps to GDB, press ``/`` in GDB" leaves both
        widgets thinking they own the bar.  When one finishes, the
        other's flag must be cleared too — otherwise keys keep
        routing to its dead search-input handler.

        *clear_other* is ``"gdb"`` to clear the gdb-scroll widget's
        flag (source-side commit/cancel) or ``"src"`` to clear the
        source widget's flag (gdb-side commit/cancel).
        """
        try:
            self.query_one("#cmdline", CommandLineBar).cancel_input()
        except NoMatches:
            pass
        if clear_other == "gdb":
            other = self._get_gdb_widget(mounted_only=True)
        else:
            other = self._get_source_view(mounted_only=True)
        if other is not None and getattr(other, "_search_active", False):
            other._search_active = False
        self._set_mode(set_mode)


    def on_search_commit(self, msg: SearchCommit) -> None:
        self._close_shared_search(set_mode="TGDB", clear_other="gdb")


    def on_search_cancel(self, msg: SearchCancel) -> None:
        self._close_shared_search(set_mode="TGDB", clear_other="gdb")


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
        self._close_shared_search(set_mode="GDB_SCROLL", clear_other="src")


    def on_scroll_search_cancel(self, msg: ScrollSearchCancel) -> None:
        self._close_shared_search(set_mode="GDB_SCROLL", clear_other="src")


    async def on_command_submit(self, msg: CommandSubmit) -> None:
        _log.info(f"command: {msg.command!r}")
        # Close any lingering completion popup.
        self._close_completion_popup()
        # If a command task is already running, reject new submissions.
        if self._cmd_task is not None and not self._cmd_task.done():
            self._show_status("Command still running (Ctrl+C to cancel)")
            return
        # Textual creates a task per async message handler; store its
        # handle so Ctrl+C (keys.py) can cancel it.  This replaces the
        # previous fire-and-forget create_task: the work runs in the
        # message-handler's own task, the handle is captured for
        # cancellation, and _run_cmd_task is awaited inline.
        self._cmd_task = asyncio.current_task()
        await self._run_cmd_task(msg.command, history_text=msg.history_text)


    async def _run_cmd_task(self, cmd: str, *, history_text: str = "") -> None:
        """Run one CommandLineBar command as an async task (one-at-a-time)."""
        try:
            cmdline = self.query_one("#cmdline", CommandLineBar)
        except NoMatches:
            self._cmd_task = None
            return

        # Record in history before execution.
        # Use verbatim history_text if provided (heredoc), otherwise cmd itself.
        hist = (history_text or cmd).strip()
        if hist:
            cmdline._add_to_history(hist, max_size=self.cfg.historysize)

        task_lock_gen = cmdline.lock_for_task()
        error_output: str | None = None
        cancelled = False

        def _print_fn(chunk: str) -> None:
            cmdline.append_output(chunk, task_gen=task_lock_gen)

        try:
            try:
                result = await self.cp.execute_async(cmd, print_fn=_print_fn)
                if result:
                    error_output = result
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                error_output = f"Internal error: {exc}"
        finally:
            # Always release the bar lock and clear the task slot, even if a
            # BaseException (KeyboardInterrupt, SystemExit) escapes the inner
            # try.  Otherwise the command line stays locked forever.
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
        self._close_completion_popup()
        self._switch_to_tgdb()


    def on_message_dismissed(self, msg: MessageDismissed) -> None:
        # Only act if still in ML_MESSAGE mode.  During map replay the synchronous
        # mode switch in _dispatch_key_internal already moved us out of ML_MESSAGE;
        # this handler becomes a no-op so it can't kill a subsequent CMD entry.
        if self._mode == "ML_MESSAGE":
            self._switch_to_tgdb()


    def on_completion_popup_show(self, msg: CompletionPopupShow) -> None:
        popup = self._get_completion_popup()
        cmdline = self._get_cmdline()
        if popup is None or cmdline is None:
            return
        bar_region = cmdline.region
        if bar_region.width <= 0:
            return
        anchor_x = bar_region.x + max(0, msg.anchor_col)
        anchor_y = bar_region.y
        popup.open(msg.items, msg.selected_idx, anchor_x, anchor_y)


    def on_completion_popup_update(self, msg: CompletionPopupUpdate) -> None:
        popup = self._get_completion_popup()
        if popup is None:
            return
        popup.set_selection(msg.selected_idx)


    def on_completion_popup_hide(self, msg: CompletionPopupHide) -> None:
        self._close_completion_popup()


    def _get_cmdline(self) -> CommandLineBar | None:
        try:
            return self.query_one("#cmdline", CommandLineBar)
        except NoMatches:
            return None


    def _get_completion_popup(self) -> CompletionPopup | None:
        try:
            return self.query_one("#completion-popup", CompletionPopup)
        except NoMatches:
            return None


    def _close_completion_popup(self) -> None:
        popup = self._get_completion_popup()
        if popup is not None:
            popup.close()


    def on_file_selected(self, msg: FileSelected) -> None:
        self._file_dialog_pending = False
        try:
            self.query_one("#file-dlg", FileDialog).close()
        except NoMatches:
            pass
        src = self._get_source_view()
        if src is not None:
            src.load_file(msg.path)
            self._update_status_file_info()
        self._switch_to_tgdb()


    def on_file_dialog_closed(self, _: FileDialogClosed) -> None:
        self._file_dialog_pending = False
        self._switch_to_tgdb()


    async def _ui_on_stopped(self, frame: Frame) -> None:
        """GDB stopped — update source view to executing location."""
        path = frame.fullname or frame.file
        _log.info(f"stopped frame={path}:{frame.line} func={frame.func}")
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

        coros: list = []
        if self._evaluate_pane is not None:
            coros.append(self._evaluate_pane.refresh_all())
        if self._disasm_pane is not None:
            current_addr = ""
            if self.gdb.current_frame is not None:
                current_addr = self.gdb.current_frame.addr
            coros.append(
                self._disasm_pane.refresh_disasm(
                    path or "",
                    frame.line,
                    current_addr=current_addr,
                    thread_id=self.gdb.current_thread_id,
                    func=frame.func,
                ),
            )
        if self._memory_panes:
            for pane in self._memory_panes:
                if pane.parent is not None:
                    coros.append(pane.refresh_memory())
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                _log.error(f"pane refresh error: {r!r}", exc_info=r)


    async def _ui_on_register_changed(self, regnum: int) -> None:
        """User wrote a register from the CLI (e.g. ``set $rax=0x1234``).

        GDB does not emit any MI async record for register writes outside
        of stop events, so without this hook the register pane would only
        catch up on the next ``*stopped``.  ``regnum`` is the affected
        register's number (or -1 for "refresh all"); we refresh the whole
        register file because it's cheap and the pane already does the
        diffing to highlight changed cells.
        """
        if self.gdb is None or self.gdb._inferior_running:
            return
        if self._register_pane is None or self._register_pane.parent is None:
            return
        try:
            await self.gdb.request_current_registers(report_error=False)
        except Exception as exc:
            _log.debug(f"register refresh failed: {exc!r}")


    def _ui_on_objfiles_changed(self) -> None:
        """A shared library was loaded/unloaded or progspace was cleared.

        We deliberately do **not** issue ``-file-list-exec-source-files``
        from this hook.  The source file list is consumed only by the
        file-dialog popup, which already re-queries lazily when it opens
        (see ``on_open_file_dialog``).  Eagerly re-querying on every
        library load was observed to trigger long debuginfod download
        cascades on systems where the dynamic linker has separate
        debuginfo (each ``=library-loaded`` could stall GDB for many
        seconds while debuginfod times out for each referenced source
        file).  Disasm is for the current frame and does not need to be
        refreshed just because an unrelated library appeared.

        The hook is kept (instead of being removed) so any future cached
        state derived from objfiles can be invalidated here without
        re-introducing the synchronous query.
        """
        return


    def _ui_on_inferior_call_pre(self) -> None:
        """User expression is about to call into the inferior.

        Mark the bottom status briefly so the user knows the inferior is
        being run by an expression like ``print foo()``.  No state-pane
        refresh here — the call hasn't happened yet.
        """
        self._show_status("inferior call …")


    async def _ui_on_inferior_call_post(self) -> None:
        """Inferior call finished — its side effects are now visible.

        ``print foo()`` can change locals, registers, and arbitrary memory.
        The inferior actually executed code, so refresh the same set of
        panes we'd refresh on ``*stopped``.  ``=memory-changed`` does fire
        for memory writes inside the call, so memory panes are already
        covered by ``on_memory_changed``; we still touch them defensively
        in case the call changed only its own locals.

        Skips when the locals pipeline is still active.  The pipeline
        spans two phases: (1) the raw ``_collect_locals()`` MI command,
        and (2) the subsequent reconciliation that issues ``-var-create``
        / ``-var-update`` for each local.  Both phases can trigger
        ``InferiorCallPostEvent`` (pretty-printers calling into the
        inferior); requesting another collect while either is running
        produces an unbounded feedback loop that eventually trips
        ``call_function_by_hand_dummy``'s ``thread_fsm`` assertion in
        ``gdb/infcall.c``.  The cancel-token check covers phase (1);
        ``_locals_pane._reconcile_active`` covers phase (2).
        """
        if self.gdb is None or self.gdb._inferior_running:
            return
        if self.gdb._locals_cancel_token in self.gdb._pending:
            return
        if self._locals_pane is not None and self._locals_pane._reconcile_active > 0:
            return

        coros: list = []
        try:
            coros.append(self.gdb.request_current_frame_locals(report_error=False))
        except Exception as exc:
            _log.debug(f"inferior-call-post locals request failed: {exc!r}")
        if self._register_pane is not None and self._register_pane.parent is not None:
            try:
                coros.append(self.gdb.request_current_registers(report_error=False))
            except Exception as exc:
                _log.debug(f"inferior-call-post registers request failed: {exc!r}")
        if self._evaluate_pane is not None:
            coros.append(self._evaluate_pane.refresh_all())
        if self._memory_panes:
            for pane in self._memory_panes:
                if pane.parent is not None:
                    coros.append(pane.refresh_memory())
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                _log.error(f"pane refresh error: {r!r}", exc_info=r)


    def _ui_on_gdb_exiting(self) -> None:
        """GDB's main loop is tearing down — start tgdb shutdown promptly.

        Without this hook, tgdb only learns GDB is gone when the primary
        PTY hits EOF, which can lag behind the user typing ``quit``.  We
        run the same teardown ``_ui_gdb_exit`` would run on EOF; it is
        idempotent so the duplicate invocation that follows when EOF
        actually arrives is a no-op.

        We must NOT just set ``_shutting_down = True`` here: ``_safe_later``
        in ``core.py`` uses that flag to drop further UI scheduling, and
        the ``on_exit`` controller callback is itself routed through
        ``_safe_later``.  Setting the flag without driving the exit would
        deadlock tgdb (logged GDB exit but never reach ``self.exit(0)``).
        """
        _log.info("GDB signaled gdb_exiting")
        self._ui_gdb_exit()


    async def _ui_on_frame_changed(self, frame: Frame) -> None:
        """The selected frame changed — update source and dependent panes.

        Fired when a ``*stopped`` async record, ``=thread-selected``
        notification, or ``request_current_location`` response reports
        a new frame.  Update the source pane to show the selected
        frame's source location (loading a new file if needed), and
        refresh the dependent panes (disasm, memory, evaluate).
        """
        path = frame.fullname or frame.file
        if path and os.path.isfile(path):
            src = self._get_source_view()
            if src is not None:
                if not src.source_file or src.source_file.path != path:
                    src.load_file(path)
                elif self.cfg.autosourcereload:
                    src.reload_if_changed()
                if frame.line > 0:
                    src.exe_line = frame.line
                    src.move_to(frame.line)
                self._update_status_file_info()

        coros: list = []
        if self._evaluate_pane is not None:
            coros.append(self._evaluate_pane.refresh_all())
        if self._disasm_pane is not None:
            current_addr = ""
            if self.gdb.current_frame is not None:
                current_addr = self.gdb.current_frame.addr
            coros.append(
                self._disasm_pane.refresh_disasm(
                    path or "",
                    frame.line,
                    current_addr=current_addr,
                    thread_id=self.gdb.current_thread_id,
                    func=frame.func,
                ),
            )
        if self._memory_panes:
            for pane in self._memory_panes:
                if pane.parent is not None:
                    coros.append(pane.refresh_memory())
        if coros:
            results = await asyncio.gather(*coros, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    _log.error(f"pane refresh error: {r!r}", exc_info=r)


    def _ui_on_memory_changed(self) -> None:
        """GDB reported =memory-changed (e.g. console-side ``print x = 2``).

        Coalesce bursts of notifications into a single refresh and re-pull
        anything that may have gone stale: locals, evaluations, and every
        mounted Memory pane. We deliberately do not touch stack / threads
        / registers — those don't depend on inferior memory contents.
        """
        if getattr(self, "_memory_changed_pending", False):
            return
        self._memory_changed_pending = True
        self.set_timer(0.05, self._flush_memory_changed)


    async def _flush_memory_changed(self) -> None:
        self._memory_changed_pending = False
        coros: list = [self.gdb.request_current_frame_locals(report_error=False)]
        if self._evaluate_pane is not None:
            coros.append(self._evaluate_pane.refresh_all())
        for pane in self._memory_panes:
            if pane.parent is not None:
                coros.append(pane.refresh_memory())
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                _log.error(f"pane refresh error: {r!r}", exc_info=r)


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


    async def _ui_set_locals(self, variables: list[LocalVariable]) -> None:
        _log.debug(f"locals: {len(variables)} vars")
        self._current_locals = list(variables)
        if self._locals_pane is not None:
            await self._locals_pane.set_variables(
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
        """Mirror cgdb startup: query current location without surfacing noise.

        Best-effort: if GDB is busy, the request may time out.  A common
        cause is attaching to a multi-threaded process via ``-pid``: GDB
        prints many ``[New LWP ...]`` lines and, with pagination enabled,
        shows ``--Type <RET> for more, q to quit, c to continue--`` which
        blocks ALL MI commands until the user responds in the console pane.
        Adding ``set pagination off`` or ``set height 0`` to ``.gdbinit``
        prevents this.

        We log the failure and continue — the user can trigger a location
        query later by stepping or setting a breakpoint.
        """
        await asyncio.sleep(0.5)
        await self.gdb.request_current_location(report_error=False)


    def _ui_gdb_exit(self) -> None:
        # Mirror cgdb: when GDB exits (EOF/error on primary PTY), exit immediately.
        # cgdb calls cgdb_cleanup_and_exit(0) in tgdb_process() on size<=0.
        # Idempotent — may be called twice when ``gdb_exiting`` event fires
        # before the PTY EOF arrives (and again from the EOF path).
        if self._shutting_down:
            return
        _log.info("GDB exited")
        self._shutting_down = True
        self._save_history_to_disk()
        self._close_inferior_tty()
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
