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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Breakpoint:
    number: int
    file: str = ""
    fullname: str = ""
    line: int = 0
    addr: str = ""
    enabled: bool = True
    temporary: bool = False
    condition: str = ""


@dataclass
class Frame:
    level: int = 0
    file: str = ""
    fullname: str = ""
    line: int = 0
    func: str = ""
    addr: str = ""


@dataclass
class LocalVariable:
    name: str = ""
    value: str = ""
    type: str = ""
    is_arg: bool = False


@dataclass
class ThreadInfo:
    id: str = ""
    target_id: str = ""
    name: str = ""
    state: str = ""
    core: str = ""
    frame: Optional[Frame] = None
    is_current: bool = False


@dataclass
class RegisterInfo:
    number: int = 0
    name: str = ""
    value: str = ""


# ---------------------------------------------------------------------------
# GDB/MI output parser
# ---------------------------------------------------------------------------

class MIParser:
    """Parse a single GDB/MI output record line."""

    @staticmethod
    def parse(line: str) -> dict:
        line = line.strip()
        out: dict = {"token": None, "type": None, "class_": None,
                     "results": {}, "raw": line}
        if not line or line == "(gdb)":
            out["type"] = "prompt"
            out["results"]["text"] = "(gdb)"
            return out

        i = 0
        while i < len(line) and line[i].isdigit():
            i += 1
        if i > 0:
            out["token"] = int(line[:i])

        if i >= len(line):
            return out

        ch = line[i]
        i += 1

        if ch in "^*+=":
            out["type"] = ch
        elif ch == "~":
            out["type"] = "console"
            out["results"]["text"] = MIParser._unescape(line[i:])
            return out
        elif ch == "@":
            out["type"] = "target"
            out["results"]["text"] = MIParser._unescape(line[i:])
            return out
        elif ch == "&":
            out["type"] = "log"
            out["results"]["text"] = MIParser._unescape(line[i:])
            return out
        else:
            out["type"] = "unknown"
            return out

        end = line.find(",", i)
        if end == -1:
            out["class_"] = line[i:]
            return out
        out["class_"] = line[i:end]
        out["results"] = MIParser._parse_results(line[end + 1:])
        return out

    @staticmethod
    def _parse_results(s: str) -> dict:
        result: dict = {}
        pos = 0
        while pos < len(s):
            m = re.match(r'(\w[\w-]*)=', s[pos:])
            if not m:
                break
            key = m.group(1)
            pos += m.end()
            val, consumed = MIParser._parse_value(s, pos)
            pos += consumed
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(val)
            else:
                result[key] = val
            if pos < len(s) and s[pos] == ',':
                pos += 1
        return result

    @staticmethod
    def _parse_value(s: str, pos: int) -> tuple:
        if pos >= len(s):
            return ("", 0)
        ch = s[pos]
        if ch == '"':
            end = pos + 1
            while end < len(s):
                if s[end] == '\\':
                    end += 2
                    continue
                if s[end] == '"':
                    end += 1
                    break
                end += 1
            return (MIParser._unescape(s[pos + 1:end - 1]), end - pos)
        elif ch == '{':
            end = pos + 1
            depth = 1
            while end < len(s) and depth > 0:
                if s[end] == '{':
                    depth += 1
                elif s[end] == '}':
                    depth -= 1
                end += 1
            return (MIParser._parse_results(s[pos + 1:end - 1]), end - pos)
        elif ch == '[':
            items = []
            end = pos + 1
            depth = 1
            while end < len(s) and depth > 0:
                if s[end] == '[':
                    depth += 1
                elif s[end] == ']':
                    depth -= 1
                end += 1
            inner = s[pos + 1:end - 1]
            p = 0
            while p < len(inner):
                if re.match(r'\w[\w-]*=', inner[p:]):
                    parsed = MIParser._parse_results(inner[p:])
                    items.append(parsed)
                    p = len(inner)
                else:
                    val, consumed = MIParser._parse_value(inner, p)
                    items.append(val)
                    p += consumed
                    if p < len(inner) and inner[p] == ',':
                        p += 1
            return (items, end - pos)
        else:
            end = pos
            while end < len(s) and s[end] not in (',', '}', ']'):
                end += 1
            return (s[pos:end], end - pos)

    @staticmethod
    def _unescape(s: str) -> str:
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        return (s.replace('\\n', '\n').replace('\\t', '\t')
                 .replace('\\"', '"').replace('\\\\', '\\'))


# ---------------------------------------------------------------------------
# GDB Controller
# ---------------------------------------------------------------------------

class GDBController:
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
        self._mi_master_fd = mi_master_fd

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
        self._mi_slave_fd = mi_slave_fd

        # Spawn GDB:
        #   --nw              : no TUI (cgdb draws its own UI)
        #   -ex "new-ui mi X" : open MI channel on secondary PTY
        cmd = [self.gdb_path, "--nw", "-ex", f"new-ui mi {mi_slave_name}"]
        cmd.extend(self.gdb_args)
        self._proc = ptyprocess.PtyProcess.spawn(cmd, dimensions=(rows, cols))

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
        rec = MIParser.parse(line)
        t = rec["type"]
        if t == "^":
            self._handle_result(rec)
        elif t in ("*", "="):
            self._handle_async(rec)
        # console/target/log/prompt on MI channel are noise — ignore

    def _handle_result(self, rec: dict) -> None:
        cls = rec.get("class_", "")
        results = rec.get("results", {})
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
                fut.set_result({"class_": cls, "results": results})

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

    def _handle_async(self, rec: dict) -> None:
        cls = rec.get("class_", "")
        results = rec.get("results", {})

        if cls == "stopped":
            self._inferior_running = False
            frame = self._parse_frame(results.get("frame", {}))
            self.current_frame = frame
            thread_id = results.get("thread-id")
            if isinstance(thread_id, str):
                self.current_thread_id = thread_id
            self.on_stopped(frame)
            self.request_current_frame_locals(report_error=False)
            self.request_current_stack_frames(report_error=False)
            self.request_current_threads(report_error=False)
            self.request_current_registers(report_error=False)
            asyncio.create_task(self._refresh_breakpoints())
        elif cls == "running":
            self._inferior_running = True
            self.locals = []
            self.on_locals([])
            self.stack = []
            self.on_stack([])
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
        elif cls == "thread-created" or cls == "thread-exited":
            if not self._inferior_running:
                self.request_current_threads(report_error=False)
        elif cls == "thread-selected":
            thread_id = results.get("id") or results.get("thread-id")
            if isinstance(thread_id, str):
                self.current_thread_id = thread_id
            self.request_current_location(report_error=False)
        elif cls == "breakpoint-modified":
            bkpt = results.get("bkpt", {})
            if bkpt:
                # _update_breakpoint_from_mi already calls on_breakpoints(); no
                # second call needed here.
                self._update_breakpoint_from_mi(bkpt)
        elif cls == "breakpoint-deleted":
            try:
                num = int(results.get("id", ""))
                self.breakpoints = [b for b in self.breakpoints if b.number != num]
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
            locals_list.append(
                LocalVariable(
                    name=str(item.get("name", "")),
                    value=value if isinstance(value, str) else "",
                    type=var_type if isinstance(var_type, str) else "",
                    is_arg=str(arg).lower() not in ("", "0", "false", "no", "n"),
                )
            )
        return locals_list

    def _parse_stack_frames(self, data) -> list[Frame]:
        frames_raw: list[dict] = []
        if isinstance(data, dict):
            raw = data.get("frame")
            if isinstance(raw, list):
                frames_raw.extend(item for item in raw if isinstance(item, dict))
            elif isinstance(raw, dict):
                frames_raw.append(raw)
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                raw = item.get("frame", item)
                if isinstance(raw, list):
                    frames_raw.extend(entry for entry in raw if isinstance(entry, dict))
                elif isinstance(raw, dict):
                    frames_raw.append(raw)
        return [self._parse_frame(item) for item in frames_raw]

    def _parse_threads(self, data) -> list[ThreadInfo]:
        threads_raw: list[dict] = []
        if isinstance(data, dict):
            raw = data.get("thread", data)
            if isinstance(raw, list):
                threads_raw.extend(item for item in raw if isinstance(item, dict))
            elif isinstance(raw, dict):
                threads_raw.append(raw)
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                raw = item.get("thread", item)
                if isinstance(raw, list):
                    threads_raw.extend(entry for entry in raw if isinstance(entry, dict))
                elif isinstance(raw, dict):
                    threads_raw.append(raw)

        threads: list[ThreadInfo] = []
        for raw in threads_raw:
            frame = raw.get("frame")
            threads.append(
                ThreadInfo(
                    id=str(raw.get("id", "")),
                    target_id=str(raw.get("target-id", "")),
                    name=str(raw.get("name", "")),
                    state=str(raw.get("state", "")),
                    core=str(raw.get("core", "")),
                    frame=self._parse_frame(frame) if isinstance(frame, dict) else None,
                    is_current=str(raw.get("id", "")) == self.current_thread_id,
                )
            )
        return threads

    def _emit_threads(self) -> None:
        for thread in self.threads:
            thread.is_current = (thread.id == self.current_thread_id)
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
            values[number] = value if isinstance(value, str) else ""
        return values

    def _emit_registers(self) -> None:
        if not self.register_names or not self._register_values:
            return

        registers: list[RegisterInfo] = []
        for number, value in sorted(self._register_values.items()):
            name = self.register_names[number] if 0 <= number < len(self.register_names) else ""
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
        existing = next((b for b in self.breakpoints if b.number == num), None)
        if existing is None:
            existing = Breakpoint(number=num)
            self.breakpoints.append(existing)
        existing.file = data.get("file", existing.file)
        existing.fullname = data.get("fullname", existing.fullname)
        existing.line = self._safe_int(data.get("line", existing.line))
        existing.addr = data.get("addr", existing.addr)
        existing.enabled = data.get("enabled", "y") == "y"
        existing.temporary = data.get("disp", "") == "del"
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
                new_bps.append(Breakpoint(
                    number=num,
                    file=bkpt_data.get("file", ""),
                    fullname=bkpt_data.get("fullname", ""),
                    line=self._safe_int(bkpt_data.get("line", 0)),
                    addr=bkpt_data.get("addr", ""),
                    enabled=bkpt_data.get("enabled", "y") == "y",
                    temporary=bkpt_data.get("disp", "") == "del",
                ))
        self.breakpoints = new_bps
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
