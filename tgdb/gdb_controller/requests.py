"""MI request helpers for ``GDBController``."""

import asyncio
import json
import logging
import os
from pathlib import Path

from ..async_util import supervise

_log = logging.getLogger("tgdb.gdb_controller")


class GDBRequestMixin:
    """Mixin providing MI command helpers and convenience requests."""

    def _send_mi_command(
        self,
        cmd: str,
        *,
        report_error: bool = True,
        kind: str | None = None,
    ) -> int | None:
        if self._mi_master_fd < 0:
            return None

        token = self._token
        self._token += 1
        self._request_meta[token] = {
            "report_error": report_error,
            "kind": kind,
        }
        raw = f"{token}{cmd}\n"
        _log.debug(f"MI->: {raw.rstrip()}")
        try:
            os.write(self._mi_master_fd, raw.encode())
        except OSError:
            self._request_meta.pop(token, None)
            return None
        return token


    def mi_command(
        self,
        cmd: str,
        *,
        report_error: bool = True,
        kind: str | None = None,
    ) -> int | None:
        return self._send_mi_command(cmd, report_error=report_error, kind=kind)


    def load_tgdb_pysetup(self, *, report_error: bool = False) -> None:
        """Load ``tgdb_pysetup.py`` into GDB's embedded Python runtime.

        Uses GDB's ``source`` command, which natively executes ``.py`` files
        as Python.  This keeps the MI command short regardless of the script
        size.
        """
        setup_path = Path(__file__).resolve().parents[1] / "tgdb_pysetup.py"
        if not setup_path.is_file():
            _log.debug(f"Skipping tgdb pysetup; file not found: {setup_path}")
            return

        _log.debug(f"Loading tgdb pysetup into GDB: {setup_path}")
        path_str = str(setup_path).replace("\\", "\\\\").replace('"', '\\"')
        console_cmd = json.dumps(f"source {path_str}")
        self.mi_command(
            f"-interpreter-exec console {console_cmd}",
            report_error=report_error,
            kind="tgdb-pysetup",
        )


    async def mi_command_async(
        self,
        cmd: str,
        timeout: float | None = 5.0,
        *,
        raise_on_error: bool = False,
    ) -> dict:
        """Send an MI command and await the decoded response.

        When ``raise_on_error`` is false, transport failures and timeouts return
        ``{}``, while ``^error`` responses are returned to the caller for
        explicit inspection. When ``raise_on_error`` is true, send failures,
        timeouts, and ``^error`` responses raise ``RuntimeError``.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        token = self._send_mi_command(cmd, report_error=False)
        if token is None:
            if raise_on_error:
                raise RuntimeError("MI channel not open")
            return {}
        self._pending[token] = future
        try:
            if timeout is None:
                result = await asyncio.shield(future)
            else:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(token, None)
            self._request_meta.pop(token, None)
            if raise_on_error:
                raise RuntimeError("MI command timed out — GDB may be busy") from exc
            return {}
        message = result.get("message", "")
        if message == "error" and raise_on_error:
            payload = result.get("payload") or {}
            if isinstance(payload, dict):
                msg = payload.get("msg", "unknown MI error")
            else:
                msg = "unknown MI error"
            raise RuntimeError(str(msg))
        return result


    def request_source_files(self) -> None:
        self.mi_command("-file-list-exec-source-files")


    def request_source_file(self, *, report_error: bool = True) -> None:
        self.mi_command("-file-list-exec-source-file", report_error=report_error)


    def request_current_location(self, *, report_error: bool = True) -> None:
        self.mi_command(
            "-stack-info-frame",
            report_error=report_error,
            kind="current-location",
        )


    def request_current_frame_locals(self, *, report_error: bool = False) -> None:
        self.mi_command(
            "-stack-list-variables --all-values",
            report_error=report_error,
            kind="stack-locals",
        )


    def request_current_stack_frames(self, *, report_error: bool = False) -> None:
        self.mi_command(
            "-stack-list-frames",
            report_error=report_error,
            kind="stack-frames",
        )


    def request_current_threads(self, *, report_error: bool = False) -> None:
        self.mi_command(
            "-thread-info",
            report_error=report_error,
            kind="thread-info",
        )


    def request_current_registers(self, *, report_error: bool = False) -> None:
        if not self.register_names:
            self.mi_command(
                "-data-list-register-names",
                report_error=report_error,
                kind="register-names",
            )
        self.mi_command(
            "-data-list-register-values x",
            report_error=report_error,
            kind="register-values",
        )


    def set_breakpoint(self, location: str, temporary: bool = False) -> None:
        flag = ""
        if temporary:
            flag = "-t "
        self.mi_command(f"-break-insert {flag}{location}")
        # Coalesce rapid set_breakpoint() calls into a single -break-list
        # refresh.  Cancel any pending refresh so only the latest debounce
        # window survives; the new task starts a fresh sleep+refresh.
        if self._break_list_task is not None and not self._break_list_task.done():
            self._break_list_task.cancel()
        self._break_list_task = supervise(
            self._delayed_break_list(),
            name="refresh-breakpoints-debounced",
        )


    async def _delayed_break_list(self) -> None:
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        self.mi_command("-break-list")


    def delete_breakpoint(self, number: int) -> None:
        self.mi_command(f"-break-delete {number}")


    def send_signal(self, signal_name: str) -> None:
        self.send_input(f"signal {signal_name}\n")


    async def read_memory_bytes_async(self, address: str, count: int = 64) -> list[dict]:
        result = await self.mi_command_async(
            f"-data-read-memory-bytes {address} {count}"
        )
        payload = result.get("payload") or {}
        raw = payload.get("memory") or []
        if isinstance(raw, dict):
            raw = [raw]
        if isinstance(raw, list):
            return raw
        return []


    async def request_disassembly_async(self, filename: str, line: int, mode: int = 1) -> list[dict]:
        result = await self.mi_command_async(
            f"-data-disassemble -f {filename} -l {line} -n -1 -- {mode}"
        )
        payload = result.get("payload") or {}
        asm = payload.get("asm_insns") or []
        if isinstance(asm, dict):
            asm = [asm]
        if isinstance(asm, list):
            return asm
        return []


    async def request_disassembly_around_pc_async(
        self, start_addr: str, span_bytes: int = 256, mode: int = 0,
    ) -> list[dict]:
        """Disassemble [start_addr, start_addr + span_bytes).

        Used when the current frame has no source file (libc, JIT, signal
        handlers) and the source-line based query cannot be issued.
        """
        if not start_addr:
            return []
        end_expr = f"{start_addr}+{span_bytes}"
        result = await self.mi_command_async(
            f"-data-disassemble -s {start_addr} -e {end_expr} -- {mode}"
        )
        payload = result.get("payload") or {}
        asm = payload.get("asm_insns") or []
        if isinstance(asm, dict):
            asm = [asm]
        if isinstance(asm, list):
            return asm
        return []


    async def request_disassembly_function_async(
        self, spec: str, mode: int = 1,
    ) -> list[dict]:
        """Disassemble the entire function containing ``spec``.

        ``spec`` may be a hex address (e.g. ``0x401120``) or a symbol name
        (e.g. ``main``). Useful for priming the disassembly pane before the
        program has been started, when no PC is available yet.
        """
        if not spec:
            return []
        result = await self.mi_command_async(
            f"-data-disassemble -a {spec} -- {mode}"
        )
        message = result.get("message", "")
        if message == "error":
            return []
        payload = result.get("payload") or {}
        asm = payload.get("asm_insns") or []
        if isinstance(asm, dict):
            asm = [asm]
        if isinstance(asm, list):
            return asm
        return []


    async def eval_expr(self, expr: str) -> str:
        escaped = expr.replace("\\", "\\\\").replace('"', '\\"')
        result = await self.mi_command_async(f'-data-evaluate-expression "{escaped}"')
        payload = result.get("payload") or {}
        message = result.get("message", "")
        if message == "error":
            if isinstance(payload, dict):
                msg = payload.get("msg", "unknown error")
            else:
                msg = "error"
            return f"<error: {msg}>"
        if isinstance(payload, dict):
            value = payload.get("value", "")
        else:
            value = str(payload)
        return str(value)
