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

import asyncio
import fcntl
import logging
import os
import signal
import termios
from collections.abc import Callable

import ptyprocess

from .requests import GDBRequestMixin
from .results import GDBResultMixin
from ..async_util import supervise
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

# Hard cap on the unparsed MI buffer.  If GDB ever emits a single line longer
# than this without a trailing newline (e.g. a runaway pretty-printer), we
# truncate it instead of letting memory grow without bound.
_MI_BUF_MAX_BYTES = 16 * 1024 * 1024


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
    - ``on_frame_changed(frame: Frame)``
    - ``on_locals(vars: list[LocalVariable])``
    - ``on_stack(frames: list[Frame])``
    - ``on_threads(threads: list[ThreadInfo])``
    - ``on_registers(registers: list[RegisterInfo])``
    - ``on_cli_prompt()``
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

        self._proc: ptyprocess.PtyProcess | None = None
        self._mi_master_fd: int = -1
        self._mi_slave_fd: int = -1  # kept open to prevent master EIO
        # Inheritable pipe used by GDB-side Python (``tgdb_pysetup.py``)
        # to notify tgdb whenever the GDB CLI is about to display a
        # prompt. Lets us refresh the source pane after CLI ``up``/
        # ``down``/``frame N``, which GDB does not broadcast over MI.
        # The write end is inherited into GDB; the read end is watched
        # by tgdb's asyncio loop.
        self._prompt_pipe_r: int = -1
        self._prompt_pipe_w: int = -1
        self._mi_buf: str = ""
        self._token: int = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._request_meta: dict[int, dict[str, object]] = {}
        # Pending debounced -break-list refresh (replaces any in-flight task
        # so rapid set_breakpoint() calls coalesce into one MI request).
        self._break_list_task: asyncio.Task | None = None
        # Epoch counter for in-flight ``_publish_locals_async`` tasks.  The
        # publish runs through several MI roundtrips after a stop; if the
        # inferior resumes (or steps to a new stop) before it finishes, the
        # in-flight snapshot would otherwise overwrite ``on_locals`` with
        # stale data once it eventually completes.  Bumped on every
        # ``*stopped`` and ``*running`` so each publish task can detect
        # that its triggering state is no longer current.
        self._locals_epoch: int = 0

        self.breakpoints: list[Breakpoint] = []
        self.source_files: list[str] = []
        self.current_frame: Frame | None = None
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
        # Fired (coalesced) whenever the GDB CLI is about to redisplay its
        # prompt — wired off the inheritable notify pipe.  Used by tgdb to
        # refresh the source pane after CLI frame navigation.
        self.on_cli_prompt: Callable[[], None] = lambda: None
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
        # Refuse double-start so we don't leak the previous PTY/pipe fds.
        # A caller that genuinely wants to restart GDB should terminate()
        # first.
        if self._proc is not None:
            raise RuntimeError("GDBController.start() called twice")

        # Create secondary PTY for MI channel.
        # Assign to self immediately so terminate() can clean up if anything below fails.
        mi_master_fd, mi_slave_fd = os.openpty()
        self._mi_master_fd = mi_master_fd
        self._mi_slave_fd = mi_slave_fd

        # Create the prompt-notify pipe. The write end is inherited by GDB
        # (via pass_fds) and clear-on-exec is removed so it survives execv.
        # Set O_NONBLOCK on the write end so a slow tgdb reader can never
        # stall GDB on a single-byte write.
        prompt_pipe_r, prompt_pipe_w = os.pipe()
        self._prompt_pipe_r = prompt_pipe_r
        self._prompt_pipe_w = prompt_pipe_w
        os.set_inheritable(prompt_pipe_w, True)
        flags = fcntl.fcntl(prompt_pipe_w, fcntl.F_GETFL)
        fcntl.fcntl(prompt_pipe_w, fcntl.F_SETFL, flags | os.O_NONBLOCK)

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

            # Spawn GDB:
            #   --nw              : no TUI
            #   -ex "new-ui mi X" : open MI channel on secondary PTY
            cmd = [self.gdb_path, "--nw", "-ex", f"new-ui mi {mi_slave_name}"]
            cmd.extend(self.gdb_args)
            self._proc = ptyprocess.PtyProcess.spawn(
                cmd,
                dimensions=(rows, cols),
                pass_fds=[prompt_pipe_w],
            )
        except Exception:
            _log.error(f"GDB spawn failed, cmd={cmd!r}")
            self.terminate()
            raise
        _log.info(f"GDB spawned, cmd={cmd!r}")


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
        for _token, future in pending:
            if not future.done():
                future.set_exception(reason)


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
        if self._prompt_pipe_r >= 0:
            fds.append(self._prompt_pipe_r)
        return fds


    def terminate(self) -> None:
        if self._proc is None:
            _log.warning("terminate() called but GDB was never started")
            return
        _log.info("GDB terminated")
        if self._proc.isalive():
            try:
                self._proc.terminate(force=True)
            except Exception:
                _log.debug("GDB terminate() raised", exc_info=True)

        # Cancel the debounced break-list refresh if one is in flight.
        # ``set_breakpoint`` schedules ``_delayed_break_list`` via
        # ``supervise``; without an explicit cancel here, that task
        # eventually wakes up after the sleep, calls ``mi_command``
        # against the now-closed MI fd (a no-op thanks to the
        # ``self._mi_master_fd < 0`` guard), and exits.  Harmless but
        # wasteful, and a fragile invariant — if the guard ever moves
        # the cleanup path becomes a real bug.  Cancel deterministically.
        if self._break_list_task is not None and not self._break_list_task.done():
            self._break_list_task.cancel()
        self._break_list_task = None

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

        for attr in ("_mi_master_fd", "_mi_slave_fd", "_prompt_pipe_r", "_prompt_pipe_w"):
            fd = getattr(self, attr, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    _log.debug(f"closing {attr} raised", exc_info=True)
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
        # Watch the event-notify pipe written by ``register_event_notify_fd``
        # inside GDB's embedded Python.  Per-tag state for coalescing.
        self._notify_buf: bytes = b""
        self._prompt_refresh_pending: bool = False
        self._objfiles_refresh_pending: bool = False
        self._dispatch_scheduled: bool = False
        # Pending register-changed regnums.  -1 means "all registers" (e.g.
        # the event lacked a regnum).
        self._pending_regnums: set[int] = set()
        if self._prompt_pipe_r >= 0:
            loop.add_reader(self._prompt_pipe_r, self._on_notify_pipe_readable)
        _log.info("MI reader started")

        # Load tgdb's embedded GDB/Python helpers before stop handling needs
        # them, then enable pretty-printing for logical varobj children.
        self.load_tgdb_pysetup(report_error=False)
        self.mi_command("-enable-pretty-printing", report_error=False)
        # Tell GDB which fd to ping on every event we care about. Sent after
        # pysetup so the helper is defined; MI commands are FIFO so ordering
        # holds.
        if self._prompt_pipe_w >= 0:
            self.mi_command(
                f'-interpreter-exec console "python register_event_notify_fd({self._prompt_pipe_w})"',
                report_error=False,
            )

        # Wait for GDB's console PTY to close (GDB exited)
        try:
            await self._console_done
        finally:
            # Clean up readers
            try:
                loop.remove_reader(self._proc.fd)
            except Exception:
                pass
            try:
                loop.remove_reader(self._mi_master_fd)
            except Exception:
                pass
            if self._prompt_pipe_r >= 0:
                try:
                    loop.remove_reader(self._prompt_pipe_r)
                except Exception:
                    pass
            _log.info("GDB exited")
            self.on_exit()




    # Cap the partial-line buffer so a runaway writer (or a peer that
    # somehow stops emitting newlines) cannot grow it without bound.
    # Notify lines are tiny (single tag plus a small int); 64 KiB is a
    # comfortable ceiling that still lets us absorb very large bursts.
    _NOTIFY_BUF_MAX_BYTES = 64 * 1024


    def _on_notify_pipe_readable(self) -> None:
        """Drain the event-notify pipe and dispatch tagged event lines.

        Each event from ``tgdb_pysetup.register_event_notify_fd`` is a
        single ``\\n``-terminated line.  We read everything available,
        accumulate a partial-line buffer between wake-ups, and then
        coalesce (so a burst of N lines results in at most one dispatch
        per tag per asyncio cycle).
        """
        try:
            while True:
                chunk = os.read(self._prompt_pipe_r, 4096)
                if not chunk:
                    self._unregister_notify_pipe()
                    return
                self._notify_buf += chunk
                if len(chunk) < 4096:
                    break
        except BlockingIOError:
            pass
        except OSError as exc:
            _log.warning(f"notify pipe read failed: {exc!r}")
            self._unregister_notify_pipe()
            return

        if len(self._notify_buf) > self._NOTIFY_BUF_MAX_BYTES:
            _log.warning(
                f"notify buffer exceeded {self._NOTIFY_BUF_MAX_BYTES} bytes; resetting"
            )
            self._notify_buf = b""
            return

        if b"\n" not in self._notify_buf:
            return
        lines = self._notify_buf.split(b"\n")
        # Last element is whatever came after the final newline (possibly
        # an incomplete line we keep for the next read).
        self._notify_buf = lines[-1]
        for raw in lines[:-1]:
            if raw:
                self._consume_notify_line(raw)

        if not self._dispatch_scheduled:
            self._dispatch_scheduled = True
            asyncio.get_running_loop().call_soon(self._dispatch_pending_events)


    def _unregister_notify_pipe(self) -> None:
        """Remove the notify-pipe asyncio reader and drop any partial line.

        Called on EOF (GDB closed its end) or on read errors that would
        otherwise busy-loop the event loop by leaving a dead fd registered.
        """
        try:
            asyncio.get_running_loop().remove_reader(self._prompt_pipe_r)
        except Exception:
            pass
        self._notify_buf = b""


    def _consume_notify_line(self, raw: bytes) -> None:
        """Update per-tag pending state for a single notify line."""
        tag = raw[:1]
        if tag == b"P":
            self._prompt_refresh_pending = True
        elif tag == b"R":
            try:
                regnum = int(raw[1:])
            except ValueError:
                regnum = -1
            # Negative values other than the documented ``-1`` sentinel
            # ("refresh all") have no meaning as regnums but ``int()``
            # accepts them happily — collapse them onto the sentinel so
            # ``-5`` and ``-1`` produce identical refresh-all dispatches
            # instead of one valid sentinel and one stray fake regnum.
            if regnum < 0:
                regnum = -1
            self._pending_regnums.add(regnum)
        elif tag in (b"O", b"F", b"C"):
            self._objfiles_refresh_pending = True
        elif tag == b"I":
            # Inferior calls aren't coalesced — pre/post are paired and
            # callers care about both edges.  Fire synchronously here so
            # ordering relative to other pending events is preserved.
            try:
                if raw == b"Ipre":
                    self.on_inferior_call_pre()
                elif raw == b"Ipost":
                    self.on_inferior_call_post()
            except Exception:
                _log.debug("inferior_call callback raised", exc_info=True)
        elif tag == b"X":
            try:
                self.on_gdb_exiting()
            except Exception:
                _log.debug("gdb_exiting callback raised", exc_info=True)


    def _dispatch_pending_events(self) -> None:
        """Fire one batch of coalesced callbacks per asyncio cycle."""
        self._dispatch_scheduled = False

        if self._objfiles_refresh_pending:
            self._objfiles_refresh_pending = False
            try:
                self.on_objfiles_changed()
            except Exception:
                _log.debug("on_objfiles_changed callback raised", exc_info=True)

        if self._pending_regnums:
            regnums = self._pending_regnums
            self._pending_regnums = set()
            # If any event lacked a regnum, treat as "refresh all" and
            # collapse to a single -1 dispatch.
            if -1 in regnums:
                regnums = {-1}
            for regnum in regnums:
                try:
                    self.on_register_changed(regnum)
                except Exception:
                    _log.debug("on_register_changed callback raised", exc_info=True)

        if self._prompt_refresh_pending:
            self._prompt_refresh_pending = False
            try:
                self.on_cli_prompt()
            except Exception:
                _log.debug("on_cli_prompt callback raised", exc_info=True)


    def _on_console_readable(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called by event loop the instant the primary PTY fd is readable."""
        try:
            data = self._proc.read(4096)
            if data:
                self.on_console(data)
        except EOFError:
            # GDB process closed — signal run_async to finish and reject
            # any in-flight MI requests so awaiters do not hang.
            loop.remove_reader(self._proc.fd)
            self._fail_pending_futures(RuntimeError("GDB process exited"))
            if not self._console_done.done():
                self._console_done.set_result(None)
        except Exception:
            loop.remove_reader(self._proc.fd)
            self._fail_pending_futures(RuntimeError("console read failed"))
            if not self._console_done.done():
                self._console_done.set_result(None)


    def _on_mi_readable(self) -> None:
        """Called by event loop the instant the MI fd is readable."""
        try:
            data = os.read(self._mi_master_fd, 4096)
            if not data:
                # MI side closed (GDB tore down the new-ui channel).  Without
                # unregistering, asyncio's level-triggered reader keeps firing
                # zero-byte callbacks on the dead fd and pins a CPU.  Reject
                # any in-flight MI futures while we're at it — no responses
                # will ever come back through this fd.
                try:
                    asyncio.get_running_loop().remove_reader(self._mi_master_fd)
                except Exception:
                    pass
                self._fail_pending_futures(RuntimeError("MI channel closed"))
                return
            self._mi_buf += data.decode("utf-8", errors="replace")
            if len(self._mi_buf) > _MI_BUF_MAX_BYTES:
                # Drop everything before the last newline so we resync on the
                # next complete record.  If there's no newline at all, drop
                # the whole buffer — the offending record cannot be parsed.
                last_nl = self._mi_buf.rfind("\n")
                dropped = (
                    len(self._mi_buf) if last_nl < 0 else last_nl
                )
                self._mi_buf = "" if last_nl < 0 else self._mi_buf[last_nl + 1:]
                _log.warning(
                    f"MI buffer exceeded {_MI_BUF_MAX_BYTES} bytes; "
                    f"discarded {dropped} bytes to resync"
                )
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
        # ``parse_response`` is a direct copy of pygdbmi and has a few
        # known issues on malformed input that would otherwise escape and
        # kill the MI reader — the loop in ``advance_past_chars`` reads
        # the buffer before bounds-checking (so a record like ``1^done,``
        # that ends right after a comma raises IndexError on the next
        # ``_parse_key``); the octal-escape decoder raises ValueError on
        # one- or two-digit ``\N`` sequences; ``_parse_mi_output`` can
        # raise on multi-record-per-line console output.  Catch any
        # parser exception here, log it, and drop the offending line so
        # one bad record does not silently end the GDB session.
        try:
            rec = GDBMIParser.parse_response(line)
        except Exception as exc:
            _log.warning(f"MI parse failed for {line[:200]!r}: {exc!r}")
            return
        t = rec["type"]
        if t == "result":
            self._handle_result(rec)
        elif t == "notify":
            supervise(self._handle_async(rec), name="mi-handle-async")
        # console/target/log/done/output on MI channel are noise — ignore
