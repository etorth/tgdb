"""Result-handling helpers for ``GDBController``."""

import logging

from .errors import GDBRequestCancelled, GDBRequestFailed, GDBRequestTimeout

_log = logging.getLogger("tgdb.gdb_controller")


class GDBResultMixin:
    """Mixin providing MI result dispatch and state publication."""

    async def _handle_result(self, rec: dict) -> None:
        cls = rec.get("message", "")
        results = rec.get("payload") or {}
        token = rec.get("token")
        meta: dict[str, object] = {}

        if token is not None:
            entry = self._pending.get(token)

            if entry is not None and entry.expect_socket:
                # Two-part completion for convenience function calls.
                mi_value = ""
                if isinstance(results, dict):
                    mi_value = results.get("value", "")
                status = mi_value.strip('"') if isinstance(mi_value, str) else ""

                if cls == "error" or status == "failed":
                    meta = self._request_meta.pop(token, {})
                    self._pending.pop(token, None)
                    if not entry.future.done():
                        msg = "gdb failed"
                        if isinstance(results, dict) and results.get("msg"):
                            msg = str(results["msg"])
                        entry.future.set_exception(GDBRequestFailed(msg))
                elif status == "cancelled":
                    meta = self._request_meta.pop(token, {})
                    self._pending.pop(token, None)
                    if not entry.future.done():
                        entry.future.set_exception(
                            GDBRequestCancelled("cancelled")
                        )
                elif status == "done":
                    entry.mi_response = rec
                    if entry.socket_response is not None:
                        # Both parts collected — resolve with socket data.
                        meta = self._request_meta.pop(token, {})
                        self._pending.pop(token, None)
                        if not entry.future.done():
                            entry.future.set_result(entry.socket_response)
                    # else: wait for socket data.
                else:
                    # Unexpected status — resolve immediately.
                    meta = self._request_meta.pop(token, {})
                    self._pending.pop(token, None)
                    if not entry.future.done():
                        entry.future.set_result(rec)

            elif entry is not None:
                # Regular MI command — resolve immediately with MI response.
                meta = self._request_meta.pop(token, {})
                self._pending.pop(token, None)
                if not entry.future.done():
                    entry.future.set_result(rec)

            else:
                # Fire-and-forget command (no entry in _pending).
                meta = self._request_meta.pop(token, {})

        _log.debug(f"MI result token={token} cls={cls}")

        if cls == "error":
            self._handle_error_result(meta, results)
        elif cls in ("done", "running"):
            await self._handle_done_result(meta, results)


    def _handle_error_result(self, meta: dict, results: dict) -> None:
        kind = meta.get("kind")
        report = bool(meta.get("report_error", True))
        if kind == "current-location":
            self.request_source_file(report_error=report)
        elif kind == "stack-locals":
            self.locals = []
            self.on_locals([])
        elif kind == "stack-frames":
            self.stack = []
            self.on_stack([])
        elif kind == "thread-info":
            if not self._inferior_running:
                self.threads = []
                self.on_threads([])
        elif kind == "register-values":
            if not self._inferior_running:
                self._register_values = {}
                self.registers = []
                self.on_registers([])
        else:
            msg = results.get("msg", "")
            if isinstance(msg, str) and report:
                _log.error(f"MI error: {msg}")
                self.on_error(msg)


    async def _handle_done_result(self, meta: dict, results: dict) -> None:
        self._handle_breakpoint_result(results)
        self._handle_source_file_result(results)
        self._handle_source_files_result(results)
        self._handle_locals_result(results)
        self._handle_stack_result(results)
        self._handle_threads_result(results)
        self._handle_register_result(results)
        await self._handle_frame_result(meta, results)


    def _handle_breakpoint_result(self, results: dict) -> None:
        breakpoint_data = results.get("bkpt")
        if breakpoint_data:
            self._update_breakpoint_from_mi(breakpoint_data)
        if "BreakpointTable" in results:
            self.handle_breaklist_result(results)


    def _handle_source_file_result(self, results: dict) -> None:
        fullname = results.get("fullname")
        if not fullname or not isinstance(fullname, str):
            return
        try:
            line = int(results.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        self.on_source_file(fullname, line)


    def _handle_source_files_result(self, results: dict) -> None:
        files = results.get("files")
        if files:
            self._handle_source_files(files)


    def _handle_locals_result(self, results: dict) -> None:
        if "variables" not in results:
            return
        self.locals = self._parse_local_variables(results.get("variables"))
        self.on_locals(list(self.locals))


    def _handle_stack_result(self, results: dict) -> None:
        if "stack" not in results:
            return
        self.stack = self._parse_stack_frames(results.get("stack"))
        self.on_stack(list(self.stack))


    def _handle_threads_result(self, results: dict) -> None:
        if "threads" not in results:
            return
        current_thread_id = results.get("current-thread-id")
        if isinstance(current_thread_id, str):
            self.current_thread_id = current_thread_id
        self.threads = self._parse_threads(results.get("threads"))
        self._emit_threads()


    def _handle_register_result(self, results: dict) -> None:
        register_names = results.get("register-names")
        if isinstance(register_names, list):
            self.register_names = []
            for name in register_names:
                if isinstance(name, str):
                    self.register_names.append(name)
                else:
                    self.register_names.append("")
            self._emit_registers()

        register_values = results.get("register-values")
        if isinstance(register_values, list):
            self._register_values = self._parse_register_values(register_values)
            self._emit_registers()


    async def _handle_frame_result(self, meta: dict, results: dict) -> None:
        kind = meta.get("kind")
        # Only the request kinds that explicitly ask for the *current
        # frame* should overwrite ``self.current_frame`` and forward to
        # the frame-changed / source-file callbacks.  ``-break-insert``
        # (and a handful of other commands on certain GDB versions) can
        # echo a ``frame={...}`` describing the breakpoint location at
        # the top level of their result payload — clobbering
        # ``current_frame`` with that value sends the rest of the app
        # chasing a phantom location and miswires every observer that
        # reads ``current_frame``.
        is_frame_query = kind == "current-location"

        frame = results.get("frame")
        report = bool(meta.get("report_error", True))
        if not frame:
            if is_frame_query:
                self.request_source_file(report_error=report)
            return

        if not is_frame_query:
            # Non-frame request happens to carry a ``frame=`` payload —
            # ignore it.  The next genuine ``current-location`` round
            # trip will populate ``current_frame`` correctly.
            return

        parsed = self._parse_frame(frame)

        if self.current_frame == parsed:
            return

        self.current_frame = parsed
        path = parsed.fullname or parsed.file
        self.on_frame_changed(parsed)
        if path:
            self.on_source_file(path, parsed.line)
        else:
            self.request_source_file(report_error=report)

        try:
            await self.request_current_frame_locals(report_error=False)
            await self.request_current_stack_frames(report_error=False)
            await self.request_current_threads(report_error=False)
            await self.request_current_registers(report_error=False)
        except (GDBRequestCancelled, GDBRequestTimeout):
            _log.debug("frame-result data collection cancelled")
