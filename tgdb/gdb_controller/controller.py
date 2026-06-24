"""
GDB controller — debugger transport plus MI request/result orchestration.

cgdb reference:
  lib/util/fork_util.cpp  — spawn GDB with --nw -ex "new-ui mi <slave>"
  lib/tgdb/tgdb.cpp       — dual-fd select loop + gdbwire MI parser

On POSIX, tgdb mirrors cgdb's two-PTY architecture:

Primary PTY  : GDB runs as a normal CLI process (--nw, no TUI).
               Raw bytes forwarded via on_console(bytes) for VT100 rendering.
Secondary PTY: GDB machine-interface channel opened via "new-ui mi <device>".
               Structured output (breakpoints, frames, source) parsed here.
               MI commands sent here; user input goes to primary PTY only.

On native Windows/UCRT64, POSIX PTYs are unavailable.  tgdb falls back to a
single MI subprocess pipe transport.  The GDB pane is line-oriented console
emulation: typed lines are submitted with "-interpreter-exec console" and MI
stream records are rendered as console output.
"""

import asyncio
import logging
import os
import secrets
import signal
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import termios
except ImportError:
    termios = None

try:
    import ptyprocess
except ImportError:
    ptyprocess = None

if TYPE_CHECKING:
    import ptyprocess as ptyprocess_types

from .requests import GDBRequestMixin
from .results import GDBResultMixin
from ..async_util import spawn_eager_task
from .types import (  # noqa: F401 — re-exported
    Breakpoint,
    Frame,
    LocalVariable,
    PendingEntry,
    quote_mi_string,
    ThreadInfo,
    RegisterInfo,
)
from .varobj import VarobjMixin
from .parsing import ParsingMixin
from .socket_data import SocketDataMixin
# GDB/MI output parser — uses GDBMIParser extracted from pygdbmi
# ---------------------------------------------------------------------------

from .miparser import GDBMIParser

_log = logging.getLogger("tgdb.gdb_controller")

# Hard cap on the unparsed MI buffer.  If GDB ever emits a single line longer
# than this without a trailing newline (e.g. a runaway pretty-printer), we
# truncate it instead of letting memory grow without bound.
_MI_BUF_MAX_BYTES = 16 * 1024 * 1024


def _posix_backend_available() -> bool:
    return (
        os.name == "posix"
        and termios is not None
        and ptyprocess is not None
        and hasattr(os, "openpty")
        and hasattr(os, "ttyname")
    )


def _encode_varint(n: int) -> bytes:
    """Encode unsigned integer *n* as LEB128 varint bytes."""
    buf = bytearray()
    while n >= 0x80:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)
    return bytes(buf)


# ---------------------------------------------------------------------------
# GDB Controller
# ---------------------------------------------------------------------------


class GDBController(GDBResultMixin, GDBRequestMixin, ParsingMixin, VarobjMixin, SocketDataMixin):
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
    - ``on_frame_changed(frame: Frame)``
    - ``on_locals(vars: list[LocalVariable])``
    - ``on_stack(frames: list[Frame])``
    - ``on_threads(threads: list[ThreadInfo])``
    - ``on_registers(registers: list[RegisterInfo])``
    - ``on_register_changed(regnum: int)``
    - ``on_objfiles_changed()``
    - ``on_inferior_call_pre()``
    - ``on_inferior_call_post()``
    - ``on_gdb_exiting()``
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
        if _posix_backend_available():
            self._backend: str = "posix"
        elif os.name == "nt":
            self._backend = "mi-pipe"
        else:
            self._backend = "unsupported"
        self._uses_socket_data = self._backend == "posix"

        self._proc: "ptyprocess_types.PtyProcess | subprocess.Popen[bytes] | None" = None
        self._mi_master_fd: int = -1
        self._mi_slave_fd: int = -1  # kept open to prevent master EIO
        # AF_UNIX socketpair used by GDB-side Python (``tgdb_pysetup.py``)
        # for all GDB↔tgdb communication: lightweight events
        # (register-changed, objfile, inferior-call, gdb-exiting) and bulk
        # data payloads (locals, stack, threads, registers).  Both share a
        # uniform binary frame format.  The GDB-side fd is inherited into
        # GDB; the tgdb-side fd is watched by tgdb's asyncio loop.
        # The socket is bidirectional: GDB writes data/events to tgdb, and
        # tgdb can write cancel tokens back to GDB.
        self._sock_tgdb: int = -1
        self._sock_gdb: int = -1
        self._tcp_listener: socket.socket | None = None
        self._tcp_data_socket: socket.socket | None = None
        self._tcp_host: str = "127.0.0.1"
        self._tcp_port: int = 0
        self._tcp_auth_token: str = ""
        self._sock_buf: bytes = b""
        self._console_buf: bytes = b""
        self._mi_buf: str = ""
        self._win_cli_buf: str = ""
        self._mi_pipe_prompt_budget: int = 1 if self._backend == "mi-pipe" else 0
        # In-flight eager-started tasks spawned by the fd-readable callbacks.
        # Tasks that complete synchronously (no real suspend) are never added.
        self._io_tasks: set[asyncio.Task] = set()
        self._pipe_tasks: set[asyncio.Task] = set()
        self._token: int = 1
        self._pending: dict[int, PendingEntry] = {}
        self._request_meta: dict[int, dict[str, object]] = {}
        self.breakpoints: list[Breakpoint] = []
        self.source_files: list[str] = []
        self.current_frame: Frame | None = None
        # Last cancel token per convenience function type.  Initialized to 0
        # (the "no cancel" sentinel — send_cancel_token(0) is a no-op).
        self._frame_cancel_token: int = 0
        self._locals_cancel_token: int = 0
        self._stack_cancel_token: int = 0
        self._registers_cancel_token: int = 0
        self._breakpoints_cancel_token: int = 0
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
        self.on_frame_changed: Callable[[Frame], None] = lambda f: None
        self.on_locals: Callable[[list[LocalVariable]], None] = lambda v: None
        self.on_stack: Callable[[list[Frame]], None] = lambda v: None
        self.on_threads: Callable[[list[ThreadInfo]], None] = lambda v: None
        self.on_registers: Callable[[list[RegisterInfo]], None] = lambda v: None
        self.on_memory_changed: Callable[[], None] = lambda: None
        self.on_exit: Callable[[], None] = lambda: None
        self.on_error: Callable[[str], None] = lambda m: None
        # User wrote a register from the CLI (``set $rax=...``).  GDB has
        # no MI async record for this; the hook lets tgdb refresh the
        # register pane immediately instead of waiting for ``*stopped``.
        # ``regnum`` is -1 if GDB did not supply one.
        self.on_register_changed: Callable[[int], None] = lambda n: None
        # A shared library was loaded, unloaded, or the program space was
        # cleared.  Coalesced into a single notification per asyncio cycle;
        # callers should re-query ``-file-list-exec-source-files``.
        self.on_objfiles_changed: Callable[[], None] = lambda: None
        # User expression triggered an inferior call (``print foo()``).
        # ``pre`` fires before the call, ``post`` after it returns.  Use
        # ``post`` to refresh locals / registers / memory, since the
        # inferior just executed arbitrary code.
        self.on_inferior_call_pre: Callable[[], None] = lambda: None
        self.on_inferior_call_post: Callable[[], None] = lambda: None
        # GDB's main loop is shutting down (e.g. user typed ``quit``).
        # Lets tgdb begin teardown without waiting for PTY EOF.
        self.on_gdb_exiting: Callable[[], None] = lambda: None



    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------


    def start(self, rows: int = 24, cols: int = 80) -> None:
        """
        Spawn GDB with dual PTYs, mirroring cgdb's fork_util.cpp.
        Primary PTY  : GDB console (user sees + types here).
        Secondary PTY: GDB MI channel via 'new-ui mi <slave_device>'.
        """
        # Refuse double-start so we don't leak the previous PTY/socket fds.
        # A caller that genuinely wants to restart GDB should terminate()
        # first.
        if self._proc is not None:
            raise RuntimeError("GDBController.start() called twice")
        if self._backend == "mi-pipe":
            self._start_mi_pipe(rows, cols)
            return
        if self._backend != "posix":
            raise RuntimeError("tgdb needs either POSIX PTYs or native Windows subprocess pipes")
        if ptyprocess is None:
            raise RuntimeError("ptyprocess is not available")
        if termios is None:
            raise RuntimeError("termios is not available")

        # Create secondary PTY for MI channel.
        # Assign to self immediately so terminate() can clean up if anything below fails.
        mi_master_fd, mi_slave_fd = os.openpty()
        self._mi_master_fd = mi_master_fd
        self._mi_slave_fd = mi_slave_fd

        # Create the AF_UNIX socketpair for all GDB↔tgdb communication.
        # The GDB-side fd is inherited by GDB (via pass_fds); the tgdb-side
        # fd is watched by asyncio.  The GDB side stays blocking so the
        # GDB-side Python retry loop can complete large data frames;
        # lightweight event frames (5 bytes) complete instantly even on a
        # blocking fd.  Enlarge the socket buffers to 1 MB so compressed
        # payloads don't stall the writer.
        sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock_a.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock_b.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except OSError:
            pass
        sock_tgdb = sock_a.detach()
        sock_gdb = sock_b.detach()
        self._sock_tgdb = sock_tgdb
        self._sock_gdb = sock_gdb
        os.set_inheritable(sock_gdb, True)
        os.set_blocking(sock_tgdb, False)

        cmd: list[str] = []
        try:
            # Disable echo on MI slave so our written commands don't echo back
            try:
                attrs = termios.tcgetattr(mi_slave_fd)
                attrs[3] &= ~(
                    termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL
                )
                termios.tcsetattr(mi_slave_fd, termios.TCSANOW, attrs)
            except Exception as exc:
                _log.warning(f"failed to disable echo on MI slave: {exc!r}")

            mi_slave_name = os.ttyname(mi_slave_fd)
            # Keep slave fd open — if we close it before GDB opens it, the master
            # immediately returns EIO (no slave reader). GDB opens its own copy.

            # Resolve pysetup path — sourced on the command line so that
            # pagination is disabled before -p <pid> triggers attach.
            setup_path = Path(__file__).resolve().parents[1] / "tgdb_pysetup.py"
            setup_path_str = str(setup_path).replace("\\", "\\\\").replace('"', '\\"')

            # Spawn GDB:
            #   --nw                     : no TUI
            #   -ex "source pysetup.py"  : load helpers + disable pagination
            #   -ex "new-ui mi X"        : open MI channel on secondary PTY
            #   <user args>              : may include -p <pid>
            #   -ex "python _tgdb_RSVD_restore_user_defs()" : restore height
            cmd = [
                self.gdb_path, "--nw",
                "-ex", f"source {setup_path_str}",
                "-ex", f"new-ui mi {mi_slave_name}",
            ]
            cmd.extend(self.gdb_args)
            cmd.extend(["-ex", "python _tgdb_RSVD_restore_user_defs()"])
            self._proc = ptyprocess.PtyProcess.spawn(
                cmd,
                dimensions=(rows, cols),
                pass_fds=[sock_gdb],
            )
        except Exception:
            _log.error(f"GDB spawn failed, cmd={cmd!r}")
            self.terminate()
            raise
        _log.info(f"GDB spawned, cmd={cmd!r}")


    def _start_mi_pipe(self, rows: int, cols: int) -> None:
        """Start native Windows GDB in MI mode over subprocess pipes."""
        self._mi_pipe_prompt_budget = 1
        cmd = [self.gdb_path, "--interpreter=mi2"]
        cmd.extend(self.gdb_args)
        self._open_tcp_side_channel()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception:
            _log.error(f"GDB spawn failed, cmd={cmd!r}")
            self.terminate()
            raise
        _log.info(f"GDB spawned, backend=mi-pipe, cmd={cmd!r}")


    def _open_tcp_side_channel(self) -> None:
        """Prepare a localhost TCP listener for GDB-side Python helpers."""
        self._close_tcp_side_channel()
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._tcp_host, 0))
            listener.listen(1)
            listener.settimeout(None)
        except OSError as exc:
            _log.warning(f"failed to open Windows tgdb TCP side channel: {exc!r}")
            return

        self._tcp_listener = listener
        self._tcp_port = listener.getsockname()[1]
        self._tcp_auth_token = secrets.token_hex(32)
        _log.debug(
            f"Windows tgdb TCP side channel listening on "
            f"{self._tcp_host}:{self._tcp_port}"
        )


    def _close_tcp_side_channel(self) -> None:
        """Close Windows TCP side-channel sockets if they are open."""
        self._uses_socket_data = self._backend == "posix"
        for attr in ("_tcp_data_socket", "_tcp_listener"):
            sock = getattr(self, attr, None)
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
            setattr(self, attr, None)
        self._tcp_port = 0
        self._tcp_auth_token = ""


    def _process_is_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        if self._backend == "posix":
            return proc.isalive()
        return proc.poll() is None


    def resize(self, rows: int, cols: int) -> None:
        if self._backend == "mi-pipe":
            return
        if self._proc and self._proc.isalive():
            self._proc.setwinsize(rows, cols)


    def is_alive(self) -> bool:
        return self._process_is_alive()


    def send_interrupt(self) -> None:
        if self._backend == "mi-pipe":
            self.mi_command("-exec-interrupt --all", report_error=False)
        elif self._proc and self._proc.isalive():
            self._proc.kill(signal.SIGINT)


    def send_input(self, data: str | bytes) -> None:
        """Write to GDB's primary PTY (user input / CLI commands)."""
        if self._backend == "mi-pipe":
            self._send_mi_pipe_input(data)
            return
        if not self._process_is_alive():
            return
        if isinstance(data, str):
            data = data.encode()
        _log.debug(f"GDB input: {data!r}")
        self._proc.write(data)


    def _send_mi_pipe_input(self, data: str | bytes) -> None:
        """Buffer line-oriented console input for the Windows MI backend."""
        if not self._process_is_alive():
            return
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = data

        idx = 0
        while idx < len(text):
            ch = text[idx]
            if ch == "\x03":
                self.send_interrupt()
            elif ch in ("\r", "\n"):
                command = self._win_cli_buf.strip()
                self._win_cli_buf = ""
                self.on_console(b"\r\n")
                if command:
                    self._send_mi_pipe_cli_command(command)
            elif ch in ("\x08", "\x7f"):
                if self._win_cli_buf:
                    self._win_cli_buf = self._win_cli_buf[:-1]
                    self.on_console(b"\b \b")
            elif ch == "\x1b":
                idx = self._skip_escape_sequence(text, idx)
                continue
            elif ch.isprintable() or ch == "\t":
                self._win_cli_buf += ch
                self.on_console(ch.encode("utf-8", errors="replace"))
            idx += 1


    @staticmethod
    def _skip_escape_sequence(text: str, idx: int) -> int:
        """Skip one terminal escape sequence in the MI-pipe input buffer."""
        idx += 1
        if idx >= len(text):
            return idx
        if text[idx] == "O":
            return min(idx + 2, len(text))
        if text[idx] != "[":
            return idx + 1
        idx += 1
        while idx < len(text):
            if text[idx].isalpha() or text[idx] == "~":
                return idx + 1
            idx += 1
        return idx


    def _send_mi_pipe_cli_command(self, command: str) -> None:
        """Submit one console command through GDB/MI."""
        _log.debug(f"GDB CLI via MI: {command!r}")
        if command in ("q", "quit"):
            self.mi_command("-gdb-exit", report_error=False)
            return
        self._mi_pipe_prompt_budget += 1
        self.mi_command(
            f"-interpreter-exec console {quote_mi_string(command)}",
            report_error=True,
            kind="console-cli",
        )


    def _mi_channel_open(self) -> bool:
        if not self._process_is_alive():
            return False
        if self._backend == "mi-pipe":
            proc = self._proc
            return proc is not None and proc.stdin is not None and not proc.stdin.closed
        return self._mi_master_fd >= 0


    def _write_mi_bytes(self, raw: bytes) -> bool:
        if self._backend == "mi-pipe":
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdin.closed:
                return False
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
                return True
            except OSError:
                return False

        try:
            written = 0
            while written < len(raw):
                n = os.write(self._mi_master_fd, raw[written:])
                if n <= 0:
                    raise OSError("os.write returned 0 bytes")
                written += n
            return True
        except OSError:
            return False


    def _next_mi_token(self) -> int:
        """Pre-allocate the next MI token.

        The returned integer serves as both the MI command prefix and the
        cancel token passed to GDB-side convenience functions.  The caller
        must pass it to ``_send_mi_command(token=...)`` so the same value
        appears on the wire.
        """
        token = self._token
        self._token += 1
        return token


    def send_cancel_token(self, token: int) -> None:
        """Write a varint-encoded cancel token to the GDB-side reader thread.

        Best-effort: if the socket is closed or the write fails, the token
        is silently dropped.  The GDB-side convenience function may complete
        before the token arrives — that is expected.
        """
        if token == 0:
            return
        if self._tcp_data_socket is not None:
            try:
                self._tcp_data_socket.sendall(_encode_varint(token))
            except OSError:
                pass
            return
        if self._sock_tgdb < 0:
            return
        try:
            os.write(self._sock_tgdb, _encode_varint(token))
        except OSError:
            pass


    def _fail_pending_futures(self, reason: BaseException) -> None:
        """Reject every in-flight ``mi_command_async`` future.

        Called when the MI channel is going away — PTY EOF or ``terminate()``.
        Without this, awaiters block until their individual timeout (or
        forever for ``timeout=None``) waiting for responses that GDB will
        never produce.  Both ``_pending`` and ``_request_meta`` are cleared
        so any late-arriving response is silently dropped on lookup.
        """
        pending = list(self._pending.items())
        self._pending.clear()
        self._request_meta.clear()
        for _token, entry in pending:
            if not entry.future.done():
                entry.future.set_exception(reason)


    def _watched_fds(self) -> list[int]:
        """Return fds that may currently have an asyncio reader attached.

        Used by ``terminate()`` to remove readers *before* closing fds —
        closing a fd that's still registered with epoll is undefined and
        can cause spurious callbacks on a recycled fd later.
        """
        fds: list[int] = []
        if self._proc is not None:
            try:
                fd = self._proc.fd
            except Exception:
                fd = -1
            if isinstance(fd, int) and fd >= 0:
                fds.append(fd)
        if self._mi_master_fd >= 0:
            fds.append(self._mi_master_fd)
        if self._sock_tgdb >= 0:
            fds.append(self._sock_tgdb)
        return fds


    def terminate(self) -> None:
        """Tear down the GDB process and every resource ``start()`` opened.

        Safe to call:

          - When ``start()`` fully succeeded — terminates the process,
            cancels io tasks, closes every fd.
          - When ``start()`` failed mid-way (``_proc`` is still None but
            the PTY / socketpair fds were already opened above the
            ``ptyprocess.spawn`` call) — still closes those fds so they
            don't leak.
          - Repeatedly — second call is a no-op.

        After ``terminate()`` returns, ``start()`` can be called again
        to spawn a fresh GDB on the same controller.
        """
        had_proc = self._proc is not None
        if had_proc:
            _log.info("GDB terminated")
            try:
                if self._backend == "mi-pipe":
                    if self._proc.poll() is None:
                        self._proc.terminate()
                elif self._proc.isalive():
                    self._proc.terminate(force=True)
            except Exception:
                _log.debug("GDB terminate() raised", exc_info=True)
        elif (
            self._mi_master_fd < 0
            and self._sock_tgdb < 0
            and self._tcp_listener is None
            and self._tcp_data_socket is None
        ):
            # Nothing was ever opened — completely idle controller.
            return

        self._close_tcp_side_channel()

        # Cancel the socket dispatch loop so it does not try to process
        # frames after the controller is torn down.
        for task in list(self._io_tasks):
            if not task.done():
                task.cancel()
        self._io_tasks.clear()
        for task in list(self._pipe_tasks):
            if not task.done():
                task.cancel()
        self._pipe_tasks.clear()

        # Wake any caller blocked in ``mi_command_async`` so they don't hang
        # on futures that will never resolve.
        self._fail_pending_futures(RuntimeError("GDB controller terminated"))

        # ``terminate()`` may be invoked outside an asyncio context (e.g.
        # from ``start()``'s except clause when spawn fails).  Tolerate the
        # no-loop case rather than crashing the cleanup path.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        # Wake ``run_async`` if it's still awaiting console EOF.  Otherwise
        # the task hangs forever because closing the fd below does not
        # synthesise an EOF callback once the reader has been removed.
        done = getattr(self, "_console_done", None)
        if loop is not None and done is not None and not done.done():
            try:
                done.set_result(None)
            except Exception:
                _log.debug("set _console_done on terminate raised", exc_info=True)

        # Remove asyncio readers BEFORE closing the underlying fds.
        # ``run_async``'s finally block also removes them, but it only runs
        # after ``_console_done`` resolves and is racy with the close calls
        # below — so do the removal here unconditionally.
        if loop is not None:
            for fd in self._watched_fds():
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass

        for attr in ("_mi_master_fd", "_mi_slave_fd", "_sock_tgdb", "_sock_gdb"):
            fd = getattr(self, attr, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    _log.debug(f"closing {attr} raised", exc_info=True)
                setattr(self, attr, -1)

        # Reset process + buffers so a subsequent ``start()`` is a clean
        # session, not a refusal.  Tokens stay incremented across
        # restarts (still globally unique within the parser).
        self._proc = None
        self._console_buf = b""
        self._sock_buf = b""
        self._mi_buf = ""
        self._win_cli_buf = ""
        self._mi_pipe_prompt_budget = 0



    # ------------------------------------------------------------------
    # Async read loops — event-driven via loop.add_reader(), no polling
    # ------------------------------------------------------------------


    async def run_async(self) -> None:
        if self._backend == "mi-pipe":
            await self._run_mi_pipe_async()
            return

        loop = asyncio.get_running_loop()

        # Use a Future to signal when GDB's primary PTY closes (EOF/error).
        # add_reader on the console fd wakes instantly when data is available,
        # matching cgdb's select()-based approach with no timeout.
        self._console_done: asyncio.Future = loop.create_future()

        # Register readable callbacks — fires as soon as the fd has data,
        # with zero polling delay (unlike asyncio.sleep(0.02)).
        # Each callback reads raw bytes into a buffer and spawns an
        # eager-started task to process complete records.  If the task
        # completes synchronously (no await), no scheduling overhead occurs.
        loop.add_reader(self._proc.fd, self._on_console_readable)
        loop.add_reader(self._mi_master_fd, self._on_mi_readable)
        # Watch the AF_UNIX socket written by ``_tgdb_RSVD_register_socket_fd`` inside GDB's
        # embedded Python.  Carries both lightweight events and bulk data.
        if self._sock_tgdb >= 0:
            loop.add_reader(self._sock_tgdb, self._on_sock_readable)
        _log.info("MI reader started")

        # pysetup is already sourced via the command line (-ex "source ...").
        # Enable pretty-printing for logical varobj children.
        self.mi_command("-enable-pretty-printing", report_error=False)
        # Tell GDB which fd to ping on every event we care about. Sent after
        # pysetup so the helper is defined; MI commands are FIFO so ordering
        # holds.
        if self._sock_gdb >= 0:
            log_enabled = _log.isEnabledFor(logging.DEBUG)
            self.mi_command(
                f'-interpreter-exec console "python _tgdb_RSVD_register_socket_fd({self._sock_gdb}, log_enabled={log_enabled})"',
                report_error=False,
            )

        # Wait for GDB's console PTY to close (GDB exited)
        try:
            await self._console_done
        finally:
            # Clean up fd readers.
            try:
                loop.remove_reader(self._proc.fd)
            except Exception:
                pass
            try:
                loop.remove_reader(self._mi_master_fd)
            except Exception:
                pass
            if self._sock_tgdb >= 0:
                try:
                    loop.remove_reader(self._sock_tgdb)
                except Exception:
                    pass
            # Cancel in-flight eager tasks.
            for task in list(self._io_tasks):
                if not task.done():
                    task.cancel()
            self._io_tasks.clear()
            _log.info("GDB exited")
            self.on_exit()


    async def _run_mi_pipe_async(self) -> None:
        """Read native Windows GDB/MI subprocess pipes."""
        loop = asyncio.get_running_loop()
        self._console_done: asyncio.Future = loop.create_future()

        proc = self._proc
        if proc is None:
            self._console_done.set_result(None)
        else:
            if proc.stdout is not None:
                task = asyncio.create_task(
                    self._read_mi_pipe_stdout(proc.stdout),
                    name="gdb-mi-pipe-stdout",
                )
                self._pipe_tasks.add(task)
                task.add_done_callback(self._pipe_tasks.discard)
            if proc.stderr is not None:
                task = asyncio.create_task(
                    self._read_mi_pipe_stderr(proc.stderr),
                    name="gdb-mi-pipe-stderr",
                )
                self._pipe_tasks.add(task)
                task.add_done_callback(self._pipe_tasks.discard)

        if self._tcp_listener is not None:
            task = asyncio.create_task(
                self._accept_tcp_side_channel(),
                name="gdb-tcp-side-channel",
            )
            self._pipe_tasks.add(task)
            task.add_done_callback(self._pipe_tasks.discard)

        self.mi_command("-gdb-set pagination off", report_error=False)
        self.mi_command("-gdb-set mi-async on", report_error=False)
        self.mi_command("-enable-pretty-printing", report_error=False)
        if self._tcp_listener is not None:
            self.load_tgdb_pysetup(report_error=False)
            log_enabled = _log.isEnabledFor(logging.DEBUG)
            register_cmd = (
                "python "
                f"_tgdb_RSVD_register_tcp_socket({self._tcp_host!r}, "
                f"{self._tcp_port}, {self._tcp_auth_token!r}, "
                f"log_enabled={log_enabled})"
            )
            self.mi_command(
                f"-interpreter-exec console {quote_mi_string(register_cmd)}",
                report_error=False,
                kind="tgdb-pysetup",
            )
            restore_cmd = "python globals().get('_tgdb_RSVD_restore_user_defs', lambda: None)()"
            self.mi_command(
                f"-interpreter-exec console {quote_mi_string(restore_cmd)}",
                report_error=False,
                kind="tgdb-pysetup",
            )

        try:
            await self._console_done
        finally:
            for task in list(self._pipe_tasks):
                if not task.done():
                    task.cancel()
            self._pipe_tasks.clear()
            for task in list(self._io_tasks):
                if not task.done():
                    task.cancel()
            self._io_tasks.clear()
            self._fail_pending_futures(RuntimeError("GDB process exited"))
            _log.info("GDB exited")
            self.on_exit()


    async def _accept_tcp_side_channel(self) -> None:
        """Accept and read the localhost TCP side channel used on Windows."""
        listener = self._tcp_listener
        expected_token = self._tcp_auth_token
        if listener is None or not expected_token:
            return

        while True:
            try:
                conn, addr = await asyncio.to_thread(listener.accept)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                _log.debug(f"Windows tgdb TCP accept stopped: {exc!r}")
                return

            token = await asyncio.to_thread(self._read_tcp_auth_token, conn)
            if token != expected_token:
                _log.warning(f"rejected unauthenticated tgdb TCP connection from {addr!r}")
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            except OSError:
                pass
            conn.settimeout(None)
            self._tcp_data_socket = conn
            self._uses_socket_data = True
            try:
                listener.close()
            except OSError:
                pass
            if self._tcp_listener is listener:
                self._tcp_listener = None
            _log.info(f"Windows tgdb TCP side channel connected from {addr!r}")
            await self._read_tcp_side_channel(conn)
            return


    @staticmethod
    def _read_tcp_auth_token(conn: socket.socket) -> str:
        """Read the one-line TCP auth token sent by GDB-side Python."""
        conn.settimeout(5.0)
        data = bytearray()
        try:
            while len(data) < 256:
                chunk = conn.recv(1)
                if not chunk:
                    break
                if chunk == b"\n":
                    break
                data.extend(chunk)
        except OSError:
            return ""
        try:
            return data.decode("ascii")
        except UnicodeDecodeError:
            return ""


    async def _read_tcp_side_channel(self, conn: socket.socket) -> None:
        """Read framed side-channel data from the accepted Windows TCP socket."""
        try:
            while True:
                try:
                    data = await asyncio.to_thread(conn.recv, 65536)
                except asyncio.CancelledError:
                    raise
                except OSError as exc:
                    _log.debug(f"Windows tgdb TCP read stopped: {exc!r}")
                    return
                if not data:
                    _log.debug("Windows tgdb TCP side channel closed")
                    return
                self._feed_sock_bytes(data)
        finally:
            if self._tcp_data_socket is conn:
                self._tcp_data_socket = None
                self._uses_socket_data = False
            try:
                conn.close()
            except OSError:
                pass


    async def _read_mi_pipe_stdout(self, pipe) -> None:
        try:
            while True:
                data = await asyncio.to_thread(pipe.readline)
                if not data:
                    break
                self._feed_mi_bytes(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.error(f"MI pipe stdout reader failed: {exc!r}", exc_info=True)
        finally:
            if not self._console_done.done():
                self._console_done.set_result(None)


    async def _read_mi_pipe_stderr(self, pipe) -> None:
        try:
            while True:
                data = await asyncio.to_thread(pipe.readline)
                if not data:
                    break
                self._console_buf += data
                self._spawn_console_processing()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug(f"MI pipe stderr reader failed: {exc!r}")


    def _on_console_readable(self) -> None:
        """Called by event loop the instant the primary PTY fd is readable."""
        try:
            data = self._proc.read(4096)
            if data:
                self._console_buf += data
                self._spawn_console_processing()
        except EOFError:
            try:
                asyncio.get_running_loop().remove_reader(self._proc.fd)
            except Exception:
                pass
            self._fail_pending_futures(RuntimeError("GDB process exited"))
            self._spawn_console_processing()
            if not self._console_done.done():
                self._console_done.set_result(None)
        except Exception as exc:
            _log.error(f"console readable failed: {exc!r}", exc_info=True)
            try:
                asyncio.get_running_loop().remove_reader(self._proc.fd)
            except Exception:
                pass
            self._fail_pending_futures(RuntimeError("console read failed"))
            self._spawn_console_processing()
            if not self._console_done.done():
                self._console_done.set_result(None)


    def _spawn_console_processing(self) -> None:
        """Spawn an eager task to forward buffered console bytes."""
        if not self._console_buf:
            return
        data = self._console_buf
        self._console_buf = b""
        spawn_eager_task(
            self._process_console_data(data),
            self._io_tasks,
            name="console-process",
        )


    async def _process_console_data(self, data: bytes) -> None:
        """Forward console bytes to the UI callback.

        Currently sync, but structured as async so future console
        processing can ``await`` if needed.
        """
        self.on_console(data)


    def _on_mi_readable(self) -> None:
        """Called by event loop the instant the MI fd is readable."""
        try:
            data = os.read(self._mi_master_fd, 4096)
            if not data:
                # MI side closed (GDB tore down the new-ui channel).
                try:
                    asyncio.get_running_loop().remove_reader(self._mi_master_fd)
                except Exception:
                    pass
                self._fail_pending_futures(RuntimeError("MI channel closed"))
                return
            self._feed_mi_bytes(data)
        except (BlockingIOError, OSError):
            pass


    def _feed_mi_bytes(self, data: bytes) -> None:
        self._mi_buf += data.decode("utf-8", errors="replace")
        if len(self._mi_buf) > _MI_BUF_MAX_BYTES:
            last_nl = self._mi_buf.rfind("\n")
            dropped = len(self._mi_buf) if last_nl < 0 else last_nl
            self._mi_buf = "" if last_nl < 0 else self._mi_buf[last_nl + 1:]
            _log.warning(
                f"MI buffer exceeded {_MI_BUF_MAX_BYTES} bytes; "
                f"discarded {dropped} bytes to resync"
            )
        self._spawn_mi_record_tasks()


    def _spawn_mi_record_tasks(self) -> None:
        """Extract complete MI lines and spawn one eager task per record."""
        while "\n" in self._mi_buf:
            line, self._mi_buf = self._mi_buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                _log.debug(f"MI<-: {line}")
            if not line:
                continue
            try:
                rec = GDBMIParser.parse_response(line)
            except Exception as exc:
                _log.warning(f"MI parse failed for {line[:200]!r}: {exc!r}")
                continue
            spawn_eager_task(
                self._dispatch_mi_record(rec),
                self._io_tasks,
                name="mi-dispatch",
            )


    async def _dispatch_mi_record(self, rec: dict) -> None:
        """Process a single parsed MI record.

        ``result`` records resolve pending futures and may trigger
        follow-up async work (e.g. data collection after a frame
        result).  ``notify`` records drive the ``*stopped`` /
        ``=thread-selected`` / etc. handlers which can freely
        ``await`` MI round-trips — each record runs in its own
        eager-started task so the fd-readable callback stays responsive.
        """
        t = rec["type"]
        if t == "result":
            await self._handle_result(rec)
        elif t == "notify":
            await self._handle_async(rec)
        elif self._backend == "mi-pipe":
            self._handle_mi_pipe_stream(rec)


    def _handle_mi_pipe_stream(self, rec: dict) -> None:
        """Render MI stream/prompt records into the Windows GDB pane."""
        record_type = rec.get("type")
        if record_type in ("console", "target", "log", "output"):
            payload = rec.get("payload", "")
            if isinstance(payload, str) and payload:
                payload = payload.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
                self._console_buf += payload.encode("utf-8", errors="replace")
                self._spawn_console_processing()
        elif record_type == "done":
            if self._mi_pipe_prompt_budget <= 0:
                return
            self._mi_pipe_prompt_budget -= 1
            self._console_buf += b"(gdb) "
            self._spawn_console_processing()
