"""Result-handling helpers for ``GDBController``."""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger("tgdb.gdb_controller")


class GDBResultMixin:
    """Mixin providing MI result dispatch and state publication."""

    def _handle_result(self, rec: dict) -> None:
        cls = rec.get("message", "")
        results = rec.get("payload") or {}
        token = rec.get("token")
        meta: dict[str, object] = {}
        if token is not None:
            meta = self._request_meta.pop(token, {})
            future = self._pending.pop(token, None)
            if future is not None and not future.done():
                future.set_result(rec)
        _log.debug(f"MI result token={token} cls={cls}")

        if cls == "error":
            self._handle_error_result(meta, results)
        elif cls in ("done", "running"):
            self._handle_done_result(meta, results)

        if token is not None and token in self._pending:
            future = self._pending.pop(token)
            if not future.done():
                future.set_result({"message": cls, "payload": results})


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


    def _handle_done_result(self, meta: dict, results: dict) -> None:
        self._handle_breakpoint_result(results)
        self._handle_source_file_result(results)
        self._handle_source_files_result(results)
        self._handle_locals_result(results)
        self._handle_stack_result(results)
        self._handle_threads_result(results)
        self._handle_register_result(results)
        self._handle_frame_result(meta, results)


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


    def _handle_frame_result(self, meta: dict, results: dict) -> None:
        frame = results.get("frame")
        report = bool(meta.get("report_error", True))
        if not frame:
            if meta.get("kind") == "current-location":
                self.request_source_file(report_error=report)
            return

        parsed = self._parse_frame(frame)
        self.current_frame = parsed
        path = parsed.fullname or parsed.file
        if path:
            self.on_source_file(path, parsed.line)
        elif meta.get("kind") == "current-location":
            self.request_source_file(report_error=report)

        if meta.get("kind") == "current-location":
            asyncio.create_task(self._publish_locals_async())
            self.request_current_stack_frames(report_error=False)
            self.request_current_threads(report_error=False)
            self.request_current_registers(report_error=False)
