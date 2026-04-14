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
import logging
import os
import signal
import termios
from typing import Callable, Optional

import ptyprocess

from .requests import GDBRequestMixin
from .results import GDBResultMixin
from .types import (  # noqa: F401 — re-exported
    Breakpoint,
    Frame,
    LocalVariable,
    ThreadInfo,
    RegisterInfo,
)
from .varobj import VarobjMixin
from .parsing import ParsingMixin
# GDB/MI output parser — uses GDBMIParser extracted from pygdbmi
# ---------------------------------------------------------------------------

from .miparser import GDBMIParser

_log = logging.getLogger("tgdb.gdb_controller")


# ---------------------------------------------------------------------------
# GDB Controller
# ---------------------------------------------------------------------------


class GDBController(GDBResultMixin, GDBRequestMixin, ParsingMixin, VarobjMixin):
    """Drive GDB through a console PTY plus a structured MI PTY.

    Public interface
    ----------------
    ``GDBController(gdb_path='gdb', args=None, init_commands=None)``
        Create the controller. Construction is side-effect free; no GDB process
        is spawned yet.

    ``start(rows=24, cols=80)``, ``terminate()``, ``run_async()``
        Manage the lifecycle of the underlying GDB process and its PTYs.

    ``send_input(data)``, ``resize(rows, cols)``, ``send_signal(sig)``
        Imperative console-facing operations for normal debugger I/O.

    ``mi_command(...)`` and the async helpers such as ``mi_command_async()``,
    ``read_memory_bytes_async()``, ``request_disassembly_async()``, and
    ``eval_expr()``
        Structured debugger operations routed through the MI channel.

    Callback contract
    -----------------
    The app injects callbacks by assigning these attributes:

    - ``on_console(data: bytes)``
    - ``on_stopped(frame: Frame)``
    - ``on_running()``
    - ``on_breakpoints(bps: list[Breakpoint])``
    - ``on_source_files(files: list[str])``
    - ``on_source_file(path: str, line: int)``
    - ``on_locals(vars: list[LocalVariable])``
    - ``on_stack(frames: list[Frame])``
    - ``on_threads(threads: list[ThreadInfo])``
    - ``on_registers(registers: list[RegisterInfo])``
    - ``on_exit()``
    - ``on_error(msg: str)``

    Callers should treat everything else as controller internals. Once started,
    the controller owns MI parsing, request bookkeeping, and publication of
    debugger state through the callback surface above.
    """

    def __init__(
        self,
        gdb_path: str = "gdb",
        args: list[str] | None = None,
        init_commands: list[str] | None = None,
    ) -> None:
        self.gdb_path = gdb_path
        self.gdb_args = args or []
        self.init_commands = init_commands or []

        self._proc: Optional[ptyprocess.PtyProcess] = None
        self._mi_master_fd: int = -1
        self._mi_slave_fd: int = -1  # kept open to prevent master EIO
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
        self.on_source_file: Callable[[str, int], None] = lambda f, ln: None
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
                attrs[3] &= ~(
                    termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL
                )
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
            _log.error(f"GDB spawn failed, cmd={cmd!r}")
            raise
        _log.info(f"GDB spawned, cmd={cmd!r}")
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


    def send_input(self, data: str | bytes) -> None:
        """Write to GDB's primary PTY (user input / CLI commands)."""
        if self._proc and self._proc.isalive():
            if isinstance(data, str):
                data = data.encode()
            _log.debug(f"GDB input: {data!r}")
            self._proc.write(data)


    def terminate(self) -> None:
        _log.info("GDB terminated")
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
    # Async read loops — event-driven via loop.add_reader(), no polling
    # ------------------------------------------------------------------

    async def run_async(self) -> None:
        loop = asyncio.get_running_loop()

        # Use a Future to signal when GDB's primary PTY closes (EOF/error).
        # add_reader on the console fd wakes instantly when data is available,
        # matching cgdb's select()-based approach with no timeout.
        self._console_done: asyncio.Future = loop.create_future()
        # _mi_done intentionally omitted — MI fd close is not monitored separately;
        # the MI reader is removed in the finally block when console closes.

        # Register readable callbacks — fires as soon as the fd has data,
        # with zero polling delay (unlike asyncio.sleep(0.02)).
        loop.add_reader(self._proc.fd, self._on_console_readable, loop)
        loop.add_reader(self._mi_master_fd, self._on_mi_readable)
        _log.info("MI reader started")

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
            _log.info("GDB exited")
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
            if line:
                _log.debug(f"MI<-: {line}")
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
