"""MI request helpers for ``GDBController``."""

import asyncio
import json
import logging
import os
from pathlib import Path

from ..async_util import supervise
from .types import PendingEntry, quote_mi_string

_log = logging.getLogger("tgdb.gdb_controller")


class GDBRequestMixin:
    """Mixin providing MI command helpers and convenience requests."""

    def _send_mi_command(
        self,
        cmd: str,
        *,
        report_error: bool = True,
        kind: str | None = None,
        token: int | None = None,
    ) -> int | None:
        if self._mi_master_fd < 0:
            return None

        if token is None:
            token = self._token
            self._token += 1
        self._request_meta[token] = {
            "report_error": report_error,
            "kind": kind,
        }
        raw = f"{token}{cmd}\n".encode()
        _log.debug(f"MI->: {raw.rstrip()!r}")
        # ``os.write`` may return fewer bytes than requested when the PTY
        # buffer fills (long expression evaluations, large sourced files,
        # etc.).  Without a retry loop the rest of the command would be
        # silently dropped and the awaiting future would never resolve
        # until its individual timeout fired — looking from the outside
        # like GDB simply hung on that command.  Loop until the full
        # buffer is delivered or os.write raises a real error.
        try:
            written = 0
            while written < len(raw):
                n = os.write(self._mi_master_fd, raw[written:])
                if n <= 0:
                    # Should not happen on a valid blocking fd, but
                    # defend against returning to the loop forever.
                    raise OSError("os.write returned 0 bytes")
                written += n
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
        token: int | None = None,
        expect_socket: bool = False,
    ) -> dict:
        """Send an MI command and await the decoded response.

        When ``raise_on_error`` is false, transport failures and timeouts return
        ``{}``, while ``^error`` responses are returned to the caller for
        explicit inspection. When ``raise_on_error`` is true, send failures,
        timeouts, and ``^error`` responses raise ``RuntimeError``.

        When *token* is provided (pre-allocated via ``_next_mi_token``),
        that value is used as the MI command prefix instead of allocating a
        new one.  This lets the caller reuse the same integer as a cancel
        token for convenience functions.

        When *expect_socket* is true, the Future waits for **both** the MI
        result and the socket data payload tagged with this token before
        resolving.  The MI value determines the outcome:

        - ``"done"``   — wait for socket data, resolve with socket payload
        - ``"failed"`` — ``RuntimeError("gdb failed")``
        - ``"cancelled"`` — ``asyncio.CancelledError("cancelled")``

        On timeout, a cancel token is sent to GDB (for ``expect_socket``
        commands) and the entry is removed from ``_pending`` so any
        late-arriving MI/socket response is silently dropped.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        token = self._send_mi_command(cmd, report_error=False, token=token)
        if token is None:
            if raise_on_error:
                raise RuntimeError("MI channel not open")
            return {}

        if expect_socket:
            self._request_meta[token]["expect_socket"] = True
        entry = PendingEntry(future=future, expect_socket=expect_socket)
        self._pending[token] = entry

        try:
            if timeout is None:
                result = await future
            else:
                result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            if expect_socket:
                self.send_cancel_token(token)
            if raise_on_error:
                raise RuntimeError("MI command timed out — GDB may be busy") from exc
            return {}
        except (RuntimeError, asyncio.CancelledError):
            # Future rejected by _fail_pending_futures (GDB exit / PTY EOF),
            # task cancelled during shutdown, or convenience function
            # returned "failed" / "cancelled".
            if raise_on_error:
                raise
            return {}
        finally:
            # Drop bookkeeping in every exit path.  These pops are no-ops
            # when the entry was already consumed by _handle_result /
            # _try_resolve_sock_pending, or cleared by _fail_pending_futures.
            self._pending.pop(token, None)
            self._request_meta.pop(token, None)

        if not expect_socket:
            message = result.get("message", "") if isinstance(result, dict) else ""
            if message == "error" and raise_on_error:
                payload = result.get("payload") or {}
                if isinstance(payload, dict):
                    msg = payload.get("msg", "unknown MI error")
                else:
                    msg = "unknown MI error"
                raise RuntimeError(str(msg))
        return result


    def _cancel_data_requests(self) -> None:
        """Cancel all in-flight convenience function requests.

        Sends cancel tokens for frame, locals, stack, registers, and
        breakpoints, then resets the stored tokens to 0 and clears the
        frame-inflight guard so new requests are not blocked.

        Called when the inferior starts running — any in-flight data
        collection is stale.
        """
        for attr in (
            "_frame_cancel_token",
            "_locals_cancel_token",
            "_stack_cancel_token",
            "_registers_cancel_token",
            "_breakpoints_cancel_token",
        ):
            token = getattr(self, attr, 0)
            self.send_cancel_token(token)
            setattr(self, attr, 0)
        self._frame_request_inflight = False


    def request_source_files(self) -> None:
        self.mi_command("-file-list-exec-source-files")


    def request_source_file(self, *, report_error: bool = True) -> None:
        self.mi_command("-file-list-exec-source-file", report_error=report_error)


    async def request_current_location(self, *, report_error: bool = True) -> None:
        self._frame_request_inflight = True
        token = self._next_mi_token()
        self.send_cancel_token(self._frame_cancel_token)
        self._frame_cancel_token = token
        await self.mi_command_async(
            f'-data-evaluate-expression "$_tgdb_RSVD_collect_frame_info({token})"',
            timeout=30.0,
            token=token,
            expect_socket=True,
        )


    async def request_current_frame_locals(self, *, report_error: bool = False) -> None:
        if self._locals_cancel_token in self._pending:
            return
        token = self._next_mi_token()
        self.send_cancel_token(self._locals_cancel_token)
        self._locals_cancel_token = token
        await self.mi_command_async(
            f'-data-evaluate-expression "$_tgdb_RSVD_collect_locals({token})"',
            timeout=30.0,
            token=token,
            expect_socket=True,
        )


    async def request_current_stack_frames(self, *, report_error: bool = False) -> None:
        token = self._next_mi_token()
        self.send_cancel_token(self._stack_cancel_token)
        self._stack_cancel_token = token
        await self.mi_command_async(
            f'-data-evaluate-expression "$_tgdb_RSVD_collect_stack({token})"',
            timeout=30.0,
            token=token,
            expect_socket=True,
        )


    async def request_current_threads(self, *, report_error: bool = False) -> None:
        # Thread info stays on MI instead of the socket-based collection path
        # used by locals/stack/registers/frame/breakpoints.  Reason: the GDB
        # Python API has no way to read another thread's stack frames without
        # calling thread.switch(), which mutates GDB's selected-thread and
        # selected-frame state.  Even with save/restore this creates ordering
        # hazards when other convenience functions run concurrently on the MI
        # queue (e.g. collect_locals seeing the wrong frame).  The C-level
        # MI command ``-thread-info`` iterates threads read-only and includes
        # per-thread frame info without touching the selected context.
        await self.mi_command_async(
            "-thread-info",
            timeout=30.0,
        )


    async def request_current_registers(self, *, report_error: bool = False) -> None:
        token = self._next_mi_token()
        self.send_cancel_token(self._registers_cancel_token)
        self._registers_cancel_token = token
        await self.mi_command_async(
            f'-data-evaluate-expression "$_tgdb_RSVD_collect_registers({token})"',
            timeout=30.0,
            token=token,
            expect_socket=True,
        )


    async def request_breakpoints(self, *, report_error: bool = False) -> None:
        token = self._next_mi_token()
        self.send_cancel_token(self._breakpoints_cancel_token)
        self._breakpoints_cancel_token = token
        await self.mi_command_async(
            f'-data-evaluate-expression "$_tgdb_RSVD_collect_breakpoints({token})"',
            timeout=30.0,
            token=token,
            expect_socket=True,
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
        await self.request_breakpoints()


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
        result = await self.mi_command_async(
            f"-data-evaluate-expression {quote_mi_string(expr)}",
        )
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
