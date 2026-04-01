"""
GDB controller — two-PTY architecture, mirroring cgdb exactly.

cgdb reference:
  lib/util/fork_util.cpp  — spawn GDB with --nw -ex "new-ui mi <slave>"
  lib/tgdb/tgdb.cpp       — dual-fd select loop + gdbwire MI parser

Primary PTY  : GDB runs as a normal CLI process (--nw, no TUI).
               Raw bytes forwarded via on_console(bytes) for VT100 rendering.
Secondary PTY: GDB machine-interface channel opened via "new-ui mi <device>".
               Structured output (breakpoints, frames, source) parsed here.
               MI commands sent here; user input goes to primary PTY only.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import termios
from dataclasses import dataclass
from typing import Callable, Optional

import ptyprocess

from .gdb_types import (  # noqa: F401 — re-exported
    Breakpoint, Frame, LocalVariable, ThreadInfo, RegisterInfo,
)
from .gdb_varobj import VarobjMixin
from .gdb_parsing import ParsingMixin
# GDB/MI output parser — uses GDBMIParser extracted from pygdbmi
# ---------------------------------------------------------------------------

from .gdb_miparser import GDBMIParser


# ---------------------------------------------------------------------------
# GDB Controller
# ---------------------------------------------------------------------------

class GDBController(ParsingMixin, VarobjMixin):
    """
    Spawn GDB with two PTY connections (primary console + MI), mirroring cgdb.

    Callbacks:
        on_console(data: bytes)              — raw PTY bytes from GDB console
        on_stopped(frame: Frame)             — execution stopped
        on_running()                         — execution resumed
        on_breakpoints(bps: list[Breakpoint])
        on_source_files(files: list[str])
        on_source_file(path: str, line: int) — current source file + line
        on_locals(vars: list[LocalVariable]) — locals in current frame
        on_stack(frames: list[Frame])        — call stack in current thread
        on_threads(threads: list[ThreadInfo]) — thread list
        on_registers(registers: list[RegisterInfo]) — register list
        on_exit()                            — GDB exited
        on_error(msg: str)                   — user-visible ^error
    """

    def __init__(self, gdb_path: str = "gdb",
                 args: list[str] | None = None,
                 init_commands: list[str] | None = None) -> None:
        self.gdb_path = gdb_path
        self.gdb_args = args or []
        self.init_commands = init_commands or []

        self._proc: Optional[ptyprocess.PtyProcess] = None
        self._mi_master_fd: int = -1
        self._mi_slave_fd: int = -1   # kept open to prevent master EIO
        self._mi_buf: str = ""
        self._token: int = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._request_meta: dict[int, dict[str, object]] = {}

        self.breakpoints: list[Breakpoint] = []
        self.source_files: list[str] = []
        self.current_frame: Optional[Frame] = None
        self.locals: list[LocalVariable] = []
        self.stack: list[Frame] = []
        self.threads: list[ThreadInfo] = []
        self.current_thread_id: str = ""
        self.registers: list[RegisterInfo] = []
        self.register_names: list[str] = []
        self._register_values: dict[int, str] = {}
        self._inferior_running: bool = False

        # Callbacks
        self.on_console: Callable[[bytes], None] = lambda d: None
        self.on_stopped: Callable[[Frame], None] = lambda f: None
        self.on_running: Callable[[], None] = lambda: None
        self.on_breakpoints: Callable[[list[Breakpoint]], None] = lambda b: None
        self.on_source_files: Callable[[list[str]], None] = lambda f: None
        self.on_source_file: Callable[[str, int], None] = lambda f, l: None
        self.on_locals: Callable[[list[LocalVariable]], None] = lambda v: None
        self.on_stack: Callable[[list[Frame]], None] = lambda v: None
        self.on_threads: Callable[[list[ThreadInfo]], None] = lambda v: None
        self.on_registers: Callable[[list[RegisterInfo]], None] = lambda v: None
        self.on_exit: Callable[[], None] = lambda: None
        self.on_error: Callable[[str], None] = lambda m: None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, rows: int = 24, cols: int = 80) -> None:
        """
        Spawn GDB with dual PTYs, mirroring cgdb's fork_util.cpp.
        Primary PTY  : GDB console (user sees + types here).
        Secondary PTY: GDB MI channel via 'new-ui mi <slave_device>'.
        """
        # Create secondary PTY for MI channel
        mi_master_fd, mi_slave_fd = os.openpty()

        try:
            # Disable echo on MI slave so our written commands don't echo back
            try:
                attrs = termios.tcgetattr(mi_slave_fd)
                attrs[3] &= ~(termios.ECHO | termios.ECHOE |
                              termios.ECHOK | termios.ECHONL)
                termios.tcsetattr(mi_slave_fd, termios.TCSANOW, attrs)
            except Exception:
                pass

            mi_slave_name = os.ttyname(mi_slave_fd)
            # Keep slave fd open — if we close it before GDB opens it, the master
            # immediately returns EIO (no slave reader). GDB opens its own copy.

            # Spawn GDB:
            #   --nw              : no TUI
            #   -ex "new-ui mi X" : open MI channel on secondary PTY
            cmd = [self.gdb_path, "--nw", "-ex", f"new-ui mi {mi_slave_name}"]
            cmd.extend(self.gdb_args)
            self._proc = ptyprocess.PtyProcess.spawn(cmd, dimensions=(rows, cols))
        except Exception:
            # Clean up PTY fds if startup fails anywhere after openpty().
            for fd in (mi_master_fd, mi_slave_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        self._mi_master_fd = mi_master_fd
        self._mi_slave_fd = mi_slave_fd

    def resize(self, rows: int, cols: int) -> None:
        if self._proc and self._proc.isalive():
            self._proc.setwinsize(rows, cols)

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.isalive())

    def send_interrupt(self) -> None:
        if self._proc and self._proc.isalive():
            self._proc.kill(signal.SIGINT)

    def send_input(self, data) -> None:
        """Write to GDB's primary PTY (user input / CLI commands)."""
        if self._proc and self._proc.isalive():
            if isinstance(data, str):
                data = data.encode()
            self._proc.write(data)

    def terminate(self) -> None:
        if self._proc and self._proc.isalive():
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
        for attr in ("_mi_master_fd", "_mi_slave_fd"):
            fd = getattr(self, attr, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    pass
                setattr(self, attr, -1)

    # ------------------------------------------------------------------
    # Async read loops (two concurrent tasks, mirrors cgdb's select loop)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Async read loops — event-driven via loop.add_reader(), no polling
    # ------------------------------------------------------------------

    async def run_async(self) -> None:
        loop = asyncio.get_event_loop()
        self._loop = loop

        # Use a Future to signal when GDB's primary PTY closes (EOF/error).
        # add_reader on the console fd wakes instantly when data is available,
        # matching cgdb's select()-based approach with no timeout.
        self._console_done: asyncio.Future = loop.create_future()
        self._mi_done: asyncio.Future = loop.create_future()

        # Register readable callbacks — fires as soon as the fd has data,
        # with zero polling delay (unlike asyncio.sleep(0.02)).
        loop.add_reader(self._proc.fd, self._on_console_readable, loop)
        loop.add_reader(self._mi_master_fd, self._on_mi_readable)

        # Enable pretty-printing so varobj operations return logical children
        # (e.g. vector elements, map key-value pairs) instead of raw internals.
        self.mi_command("-enable-pretty-printing", report_error=False)

        # Wait for GDB's console PTY to close (GDB exited)
        try:
            await self._console_done
        finally:
            # Clean up both readers
            try:
                loop.remove_reader(self._proc.fd)
            except Exception:
                pass
            try:
                loop.remove_reader(self._mi_master_fd)
            except Exception:
                pass
            self.on_exit()

    def _on_console_readable(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called by event loop the instant the primary PTY fd is readable."""
        try:
            data = self._proc.read(4096)
            if data:
                self.on_console(data)
        except EOFError:
            # GDB process closed — signal run_async to finish
            loop.remove_reader(self._proc.fd)
            if not self._console_done.done():
                self._console_done.set_result(None)
        except Exception:
            loop.remove_reader(self._proc.fd)
            if not self._console_done.done():
                self._console_done.set_result(None)

    def _on_mi_readable(self) -> None:
        """Called by event loop the instant the MI fd is readable."""
        try:
            data = os.read(self._mi_master_fd, 4096)
            if not data:
                return
            self._mi_buf += data.decode("utf-8", errors="replace")
            self._process_mi_buffer()
        except (BlockingIOError, OSError):
            pass

    def _process_mi_buffer(self) -> None:
        while "\n" in self._mi_buf:
            line, self._mi_buf = self._mi_buf.split("\n", 1)
            line = line.rstrip("\r")
            self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        if not line:
            return
        rec = GDBMIParser.parse_response(line)
        t = rec["type"]
        if t == "result":
            self._handle_result(rec)
        elif t == "notify":
            self._handle_async(rec)
        # console/target/log/done/output on MI channel are noise — ignore

    def _handle_result(self, rec: dict) -> None:
        cls = rec.get("message", "")
        results = rec.get("payload") or {}
        token = rec.get("token")
        meta: dict[str, object] = {}
        if token is not None:
            meta = self._request_meta.pop(token, {})

        if cls == "error":
            if meta.get("kind") == "current-location":
                self.request_source_file(report_error=bool(meta.get("report_error", True)))
            elif meta.get("kind") == "stack-locals":
                self.locals = []
                self.on_locals([])
            elif meta.get("kind") == "stack-frames":
                self.stack = []
                self.on_stack([])
            elif meta.get("kind") == "thread-info":
                if not self._inferior_running:
                    self.threads = []
                    self.on_threads([])
            elif meta.get("kind") == "register-values":
                if not self._inferior_running:
                    self._register_values = {}
                    self.registers = []
                    self.on_registers([])
            else:
                msg = results.get("msg", "")
                if isinstance(msg, str) and bool(meta.get("report_error", True)):
                    self.on_error(msg)

        elif cls in ("done", "running"):
            bkpt = results.get("bkpt")
            if bkpt:
                self._update_breakpoint_from_mi(bkpt)
            if "BreakpointTable" in results:
                self.handle_breaklist_result(results)
            fullname = results.get("fullname")
            if fullname and isinstance(fullname, str):
                try:
                    line = int(results.get("line", 0) or 0)
                except (TypeError, ValueError):
                    line = 0
                self.on_source_file(fullname, line)
            files = results.get("files")
            if files:
                self._handle_source_files(files)
            if "variables" in results:
                self.locals = self._parse_local_variables(results.get("variables"))
                self.on_locals(list(self.locals))
            if "stack" in results:
                self.stack = self._parse_stack_frames(results.get("stack"))
                self.on_stack(list(self.stack))
            if "threads" in results:
                current_thread_id = results.get("current-thread-id")
                if isinstance(current_thread_id, str):
                    self.current_thread_id = current_thread_id
                self.threads = self._parse_threads(results.get("threads"))
                self._emit_threads()
            register_names = results.get("register-names")
            if isinstance(register_names, list):
                self.register_names = [name if isinstance(name, str) else "" for name in register_names]
                self._emit_registers()
            register_values = results.get("register-values")
            if isinstance(register_values, list):
                self._register_values = self._parse_register_values(register_values)
                self._emit_registers()
            frame = results.get("frame")
            if frame:
                parsed = self._parse_frame(frame)
                self.current_frame = parsed
                path = parsed.fullname or parsed.file
                if path:
                    self.on_source_file(path, parsed.line)
                elif meta.get("kind") == "current-location":
                    self.request_source_file(report_error=bool(meta.get("report_error", True)))
                if meta.get("kind") == "current-location":
                    self.request_current_frame_locals(report_error=False)
                    self.request_current_stack_frames(report_error=False)
                    self.request_current_threads(report_error=False)
                    self.request_current_registers(report_error=False)
            elif meta.get("kind") == "current-location":
                # Mirror cgdb's startup query path: ask for the current frame
                # first, then fall back to exec source-file if needed.
                self.request_source_file(report_error=bool(meta.get("report_error", True)))

        if token is not None and token in self._pending:
            fut = self._pending.pop(token)
            if not fut.done():
                fut.set_result({"message": cls, "payload": results})

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
        try:
            os.write(self._mi_master_fd, f"{token}{cmd}\n".encode())
        except OSError:
            self._request_meta.pop(token, None)
            return None
        return token

    # ------------------------------------------------------------------
    # MI command helpers (sent on MI channel, not primary console)
    # ------------------------------------------------------------------

    def mi_command(self, cmd: str, *, report_error: bool = True,
                   kind: str | None = None) -> int | None:
        return self._send_mi_command(cmd, report_error=report_error, kind=kind)


    # ------------------------------------------------------------------
    # Varobj commands — structured variable inspection
    # ------------------------------------------------------------------


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

    async def _refresh_breakpoints(self) -> None:
        self.mi_command("-break-list")

    def set_breakpoint(self, location: str, temporary: bool = False) -> None:
        flag = "-t " if temporary else ""
        self.mi_command(f"-break-insert {flag}{location}")
        asyncio.create_task(self._delayed_break_list())

    async def _delayed_break_list(self) -> None:
        await asyncio.sleep(0.1)
        self.mi_command("-break-list")

    def delete_breakpoint(self, number: int) -> None:
        self.mi_command(f"-break-delete {number}")

    def enable_breakpoint(self, number: int) -> None:
        self.mi_command(f"-break-enable {number}")

    def disable_breakpoint(self, number: int) -> None:
        self.mi_command(f"-break-disable {number}")


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------


