"""
GDB/MI parsing helpers used by the controller package.

Provides ``ParsingMixin``, which handles async notification dispatch,
MI result parsing (frames, locals, threads, registers, breakpoints,
source files), and the ``_safe_int`` utility. Mixed into ``GDBController``.
"""

import asyncio
import logging

from .types import (
    Breakpoint,
    Frame,
    LocalVariable,
    RegisterInfo,
    ThreadInfo,
)

_log = logging.getLogger("tgdb.gdb_parsing")


class ParsingMixin:
    """Mixin providing MI result parsing and async notification handling.

    Expects the host class to have the state attributes and callbacks
    defined in ``GDBController.__init__``, as well as ``mi_command()``
    and the various ``request_*()`` helpers.
    """

    # ------------------------------------------------------------------
    # Async (notify) record handler
    # ------------------------------------------------------------------

    async def _handle_async(self, rec: dict) -> None:
        cls = rec.get("message", "")
        results = rec.get("payload") or {}

        if cls == "stopped":
            self._inferior_running = False
            frame = self._parse_frame(results.get("frame", {}))
            self.current_frame = frame
            thread_id = results.get("thread-id")
            if isinstance(thread_id, str):
                self.current_thread_id = thread_id
            reason = results.get("reason", "")
            path = frame.file or frame.fullname
            _log.info(f"stopped reason={reason} frame={path}:{frame.line}")
            self.on_stopped(frame)
            self.request_current_stack_frames(report_error=False)
            self.request_current_threads(report_error=False)
            self.request_current_registers(report_error=False)
            self.mi_command("-break-list")
        elif cls == "running":
            self._inferior_running = True
            self.current_frame = None
            self.locals = []
            self.stack = []
            self.on_stack([])
            _log.info("running")
            running_thread = results.get("thread-id")
            if self.threads:
                if running_thread == "all":
                    for thread in self.threads:
                        thread.state = "running"
                elif isinstance(running_thread, str):
                    for thread in self.threads:
                        if thread.id == running_thread:
                            thread.state = "running"
                self._emit_threads()
            self.on_running()
        elif cls in ("thread-created", "thread-exited"):
            if not self._inferior_running:
                self.request_current_threads(report_error=False)
        elif cls == "thread-selected":
            thread_id = results.get("id") or results.get("thread-id")
            if isinstance(thread_id, str):
                self.current_thread_id = thread_id
            self.request_current_location(report_error=False)
        elif cls == "memory-changed":
            _log.info(
                f"memory-changed addr={results.get('addr', '?')} "
                f"len={results.get('len', '?')}"
            )
            self.on_memory_changed()
        elif cls == "breakpoint-modified":
            bkpt = results.get("bkpt", {})
            if bkpt:
                # _update_breakpoint_from_mi already calls on_breakpoints(); no
                # second call needed here.
                self._update_breakpoint_from_mi(bkpt)
        elif cls == "breakpoint-deleted":
            try:
                num = int(results.get("id", ""))
                _log.info(f"breakpoint-deleted id={results.get('id', '')}")
                kept = []
                for b in self.breakpoints:
                    if b.number != num:
                        kept.append(b)
                self.breakpoints = kept
                self.on_breakpoints(list(self.breakpoints))
            except (ValueError, TypeError):
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_frame(self, data: dict) -> Frame:
        if not isinstance(data, dict):
            return Frame()
        return Frame(
            level=self._safe_int(data.get("level", 0)),
            file=data.get("file", ""),
            fullname=data.get("fullname", ""),
            line=self._safe_int(data.get("line", 0)),
            func=data.get("func", ""),
            addr=data.get("addr", ""),
        )


    def _parse_local_variables(self, data) -> list[LocalVariable]:
        if not isinstance(data, list):
            return []

        locals_list: list[LocalVariable] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            arg = item.get("arg", 0)
            value = item.get("value")
            var_type = item.get("type")
            if isinstance(value, str):
                value_str = value
            else:
                value_str = ""
            if isinstance(var_type, str):
                type_str = var_type
            else:
                type_str = ""
            locals_list.append(
                LocalVariable(
                    name=str(item.get("name", "")),
                    value=value_str,
                    type=type_str,
                    is_arg=str(arg).lower() not in ("", "0", "false", "no", "n"),
                )
            )
        return locals_list


    def _parse_stack_frames(self, data) -> list[Frame]:
        frames_raw: list[dict] = []
        if isinstance(data, dict):
            raw = data.get("frame")
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        frames_raw.append(item)
            elif isinstance(raw, dict):
                frames_raw.append(raw)
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                raw = item.get("frame", item)
                if isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict):
                            frames_raw.append(entry)
                elif isinstance(raw, dict):
                    frames_raw.append(raw)
        frames = []
        for item in frames_raw:
            frames.append(self._parse_frame(item))
        return frames


    def _parse_threads(self, data) -> list[ThreadInfo]:
        threads_raw: list[dict] = []
        if isinstance(data, dict):
            raw = data.get("thread", data)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        threads_raw.append(item)
            elif isinstance(raw, dict):
                threads_raw.append(raw)
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                raw = item.get("thread", item)
                if isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict):
                            threads_raw.append(entry)
                elif isinstance(raw, dict):
                    threads_raw.append(raw)

        threads: list[ThreadInfo] = []
        for raw in threads_raw:
            frame = raw.get("frame")
            if isinstance(frame, dict):
                parsed_frame = self._parse_frame(frame)
            else:
                parsed_frame = None
            threads.append(
                ThreadInfo(
                    id=str(raw.get("id", "")),
                    target_id=str(raw.get("target-id", "")),
                    name=str(raw.get("name", "")),
                    state=str(raw.get("state", "")),
                    core=str(raw.get("core", "")),
                    frame=parsed_frame,
                    is_current=str(raw.get("id", "")) == self.current_thread_id,
                )
            )
        return threads


    def _emit_threads(self) -> None:
        for thread in self.threads:
            thread.is_current = thread.id == self.current_thread_id
        self.on_threads(list(self.threads))


    def _parse_register_values(self, data) -> dict[int, str]:
        values: dict[int, str] = {}
        if not isinstance(data, list):
            return values
        for item in data:
            if not isinstance(item, dict):
                continue
            number = self._safe_int(item.get("number", -1))
            if number < 0:
                continue
            value = item.get("value")
            if isinstance(value, str):
                values[number] = value
            else:
                values[number] = ""
        return values


    def _emit_registers(self) -> None:
        if not self.register_names or not self._register_values:
            return

        registers: list[RegisterInfo] = []
        for number, value in sorted(self._register_values.items()):
            if 0 <= number < len(self.register_names):
                name = self.register_names[number]
            else:
                name = ""
            if not name:
                continue
            registers.append(RegisterInfo(number=number, name=name, value=value))
        self.registers = registers
        self.on_registers(list(self.registers))


    def _update_breakpoint_from_mi(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        num = self._safe_int(data.get("number", 0))
        if num == 0:
            return
        existing = None
        for b in self.breakpoints:
            if b.number == num:
                existing = b
                break
        if existing is None:
            existing = Breakpoint(number=num)
            self.breakpoints.append(existing)
        existing.file = data.get("file", existing.file)
        existing.fullname = data.get("fullname", existing.fullname)
        existing.line = self._safe_int(data.get("line", existing.line))
        existing.addr = data.get("addr", existing.addr)
        if "enabled" in data:
            existing.enabled = data["enabled"] == "y"
        existing.temporary = data.get("disp", "") == "del"
        path = existing.file or existing.fullname
        _log.info(f"breakpoint updated: #{existing.number} {path}:{existing.line}")
        self.on_breakpoints(list(self.breakpoints))


    def handle_breaklist_result(self, results: dict) -> None:
        body = results.get("BreakpointTable", {})
        if not isinstance(body, dict):
            return
        bkpts_raw = body.get("body", [])
        if not isinstance(bkpts_raw, list):
            return
        new_bps: list[Breakpoint] = []
        for raw in bkpts_raw:
            if not isinstance(raw, dict):
                continue
            bkpt_data = raw.get("bkpt", raw)
            num = self._safe_int(bkpt_data.get("number", 0))
            if num:
                new_bps.append(
                    Breakpoint(
                        number=num,
                        file=bkpt_data.get("file", ""),
                        fullname=bkpt_data.get("fullname", ""),
                        line=self._safe_int(bkpt_data.get("line", 0)),
                        addr=bkpt_data.get("addr", ""),
                        enabled=bkpt_data.get("enabled", "y") == "y",
                        temporary=bkpt_data.get("disp", "") == "del",
                    )
                )
        self.breakpoints = new_bps
        _log.info(f"breaklist: {len(new_bps)} breakpoints")
        self.on_breakpoints(list(self.breakpoints))


    def _handle_source_files(self, files) -> None:
        if isinstance(files, list):
            paths: list[str] = []
            seen: set[str] = set()
            for f in files:
                if isinstance(f, dict):
                    p = f.get("fullname") or f.get("file", "")
                    if p and p not in seen:
                        seen.add(p)
                        paths.append(p)
                elif isinstance(f, str):
                    if f and f not in seen:
                        seen.add(f)
                        paths.append(f)
            self.source_files = paths
            self.on_source_files(list(self.source_files))


    @staticmethod
    def _safe_int(val) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
