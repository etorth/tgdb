"""
GDB controller — manages the GDB subprocess via a PTY.

Communicates over GDB/MI (--interpreter=mi2).  Parses MI output for
breakpoints, frames, source positions, and file lists.  Console output
(the "human-readable" stream) is forwarded separately to the GDB widget.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
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
    file: str = ""
    fullname: str = ""
    line: int = 0
    func: str = ""
    addr: str = ""


# ---------------------------------------------------------------------------
# Minimal GDB/MI output parser
# ---------------------------------------------------------------------------

class MIParser:
    """
    Parse a single GDB/MI output record line.

    GDB/MI output format:
        token? type-char result-class ( ',' variable '=' value )* nl
    type chars: '^' (result), '*' (async exec), '=' (notify), '~' (console),
                '@' (target), '&' (log), '(gdb)' prompt.
    """

    _re_kv = re.compile(r'(\w[\w-]*)=')

    @staticmethod
    def parse(line: str) -> dict:
        """Return a dict with keys: token, type, class_, results, raw."""
        line = line.strip()
        out: dict = {"token": None, "type": None, "class_": None,
                     "results": {}, "raw": line}
        if not line or line == "(gdb)":
            out["type"] = "prompt"
            out["results"]["text"] = "(gdb)"
            return out

        i = 0
        # Optional numeric token
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

        # Result class
        end = line.find(",", i)
        if end == -1:
            out["class_"] = line[i:]
            return out
        out["class_"] = line[i:end]
        i = end + 1

        # Parse key=value pairs
        out["results"] = MIParser._parse_results(line[i:])
        return out

    @staticmethod
    def _parse_results(s: str) -> dict:
        """Recursively parse MI result list."""
        result: dict = {}
        pos = 0
        while pos < len(s):
            # Find key
            m = re.match(r'(\w[\w-]*)=', s[pos:])
            if not m:
                break
            key = m.group(1)
            pos += m.end()
            val, consumed = MIParser._parse_value(s, pos)
            pos += consumed
            # Support repeated keys → list
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
        """Return (value, chars_consumed)."""
        if pos >= len(s):
            return ("", 0)
        ch = s[pos]
        if ch == '"':
            # C-string
            end = pos + 1
            while end < len(s):
                if s[end] == '\\':
                    end += 2
                    continue
                if s[end] == '"':
                    end += 1
                    break
                end += 1
            return (MIParser._unescape(s[pos+1:end-1]), end - pos)
        elif ch == '{':
            # Tuple
            end = pos + 1
            depth = 1
            while end < len(s) and depth > 0:
                if s[end] == '{':
                    depth += 1
                elif s[end] == '}':
                    depth -= 1
                end += 1
            inner = s[pos+1:end-1]
            return (MIParser._parse_results(inner), end - pos)
        elif ch == '[':
            # List
            items = []
            end = pos + 1
            depth = 1
            while end < len(s) and depth > 0:
                if s[end] == '[':
                    depth += 1
                elif s[end] == ']':
                    depth -= 1
                end += 1
            inner = s[pos+1:end-1]
            # Items may be values or key=value pairs
            p = 0
            while p < len(inner):
                if re.match(r'\w[\w-]*=', inner[p:]):
                    parsed = MIParser._parse_results(inner[p:])
                    items.append(parsed)
                    # Skip past the parsed item (rough)
                    for v in parsed.values():
                        break
                    p = len(inner)  # simplified
                else:
                    val, consumed = MIParser._parse_value(inner, p)
                    items.append(val)
                    p += consumed
                    if p < len(inner) and inner[p] == ',':
                        p += 1
            return (items, end - pos)
        else:
            # Bare word (shouldn't happen in well-formed MI, but be defensive)
            end = pos
            while end < len(s) and s[end] not in (',', '}', ']'):
                end += 1
            return (s[pos:end], end - pos)

    @staticmethod
    def _unescape(s: str) -> str:
        """Unescape a GDB/MI C-style string (strip outer quotes if present)."""
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        return (s.replace('\\n', '\n')
                  .replace('\\t', '\t')
                  .replace('\\"', '"')
                  .replace('\\\\', '\\'))


# ---------------------------------------------------------------------------
# GDB Controller
# ---------------------------------------------------------------------------

class GDBController:
    """
    Spawn GDB, drive it via MI, expose callbacks for UI events.

    Callbacks (set as attributes, all optional):
        on_console(text: str)              — GDB console output (stream ~)
        on_prompt(text: str)               — GDB prompt
        on_target(text: str)               — debuggee stdout (@)
        on_log(text: str)                  — GDB log (&)
        on_stopped(frame: Frame)           — execution stopped
        on_running()                       — execution resumed
        on_breakpoints(bps: list[Breakpoint])
        on_source_files(files: list[str])
        on_exit()                          — GDB exited
        on_error(msg: str)                 — ^error
    """

    def __init__(self, gdb_path: str = "gdb", args: list[str] | None = None,
                 init_commands: list[str] | None = None) -> None:
        self.gdb_path = gdb_path
        self.gdb_args = args or []
        self.init_commands = init_commands or []
        self._proc: Optional[ptyprocess.PtyProcess] = None
        self._token = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._read_task: Optional[asyncio.Task] = None
        self._buf = ""
        self.breakpoints: list[Breakpoint] = []
        self.source_files: list[str] = []
        self.current_frame: Optional[Frame] = None
        # Callbacks
        self.on_console: Callable[[str], None] = lambda t: None
        self.on_prompt: Callable[[str], None] = lambda t: None
        self.on_partial: Callable[[str], None] = lambda t: None
        self.on_target: Callable[[str], None] = lambda t: None
        self.on_log: Callable[[str], None] = lambda t: None
        self.on_stopped: Callable[[Frame], None] = lambda f: None
        self.on_running: Callable[[], None] = lambda: None
        self.on_breakpoints: Callable[[list[Breakpoint]], None] = lambda b: None
        self.on_source_files: Callable[[list[str]], None] = lambda f: None
        self.on_source_file: Callable[[str], None] = lambda f: None
        self.on_exit: Callable[[], None] = lambda: None
        self.on_error: Callable[[str], None] = lambda m: None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, rows: int = 24, cols: int = 80) -> None:
        """Spawn GDB process on a PTY."""
        cmd = [self.gdb_path, "--interpreter=mi2", "--quiet"]
        cmd.extend(self.gdb_args)
        self._proc = ptyprocess.PtyProcess.spawn(
            cmd, dimensions=(rows, cols)
        )

    def resize(self, rows: int, cols: int) -> None:
        if self._proc and self._proc.isalive():
            self._proc.setwinsize(rows, cols)

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.isalive())

    def send_interrupt(self) -> None:
        """Send Ctrl-C to GDB."""
        if self._proc and self._proc.isalive():
            self._proc.kill(signal.SIGINT)

    def send_input(self, text: str) -> None:
        """Write raw text to GDB's stdin (PTY)."""
        if self._proc and self._proc.isalive():
            self._proc.write(text.encode())

    # ------------------------------------------------------------------
    # Async read loop
    # ------------------------------------------------------------------

    async def run_async(self) -> None:
        """Async read loop — call this as a background task."""
        self._loop = asyncio.get_event_loop()
        loop = self._loop
        try:
            while self._proc and self._proc.isalive():
                try:
                    data = await loop.run_in_executor(
                        None, self._read_chunk
                    )
                    if data:
                        self._buf += data
                        self._process_buffer()
                except EOFError:
                    break
                except Exception:
                    break
        finally:
            self.on_exit()

    def _read_chunk(self) -> str:
        """Blocking read from PTY fd — runs in executor."""
        try:
            raw = self._proc.read(4096)
            return raw.decode("utf-8", errors="replace")
        except EOFError:
            raise
        except Exception:
            return ""

    def _process_buffer(self) -> None:
        """Split buffer on newlines and dispatch each complete line."""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            self._dispatch(line)
        # Forward whatever's left as the current partial line (readline echo
        # from user typing comes back as raw PTY bytes without a terminating \n).
        if self._buf:
            self.on_partial(self._buf)

    def _dispatch(self, line: str) -> None:
        """Parse and dispatch one MI line."""
        if not line:
            return
        rec = MIParser.parse(line)
        t = rec["type"]

        if t == "console":
            self.on_console(rec["results"].get("text", ""))
        elif t == "prompt":
            self.on_prompt(rec["results"].get("text", "(gdb)"))
        elif t == "target":
            self.on_target(rec["results"].get("text", ""))
        elif t == "log":
            self.on_log(rec["results"].get("text", ""))
        elif t == "^":
            self._handle_result(rec)
        elif t in ("*", "="):
            self._handle_async(rec)
        # unknown ignored

    def _handle_result(self, rec: dict) -> None:
        cls = rec.get("class_", "")
        results = rec.get("results", {})
        token = rec.get("token")

        if cls == "error":
            msg = results.get("msg", "")
            if isinstance(msg, str):
                self.on_error(msg)
        elif cls in ("done", "running"):
            # Handle breakpoint creation
            bkpt = results.get("bkpt")
            if bkpt:
                self._update_breakpoint_from_mi(bkpt)
            # Handle -break-list
            if "BreakpointTable" in results:
                self.handle_breaklist_result(results)
            # Handle -file-list-exec-source-file (singular)
            fullname = results.get("fullname")
            if fullname and isinstance(fullname, str):
                self.on_source_file(fullname)
            # Handle -file-list-exec-source-files (plural)
            files = results.get("files")
            if files:
                self._handle_source_files(files)
            # Handle -stack-info-frame
            frame = results.get("frame")
            if frame:
                self.current_frame = self._parse_frame(frame)

        # Resolve pending future
        if token is not None and token in self._pending:
            fut = self._pending.pop(token)
            if not fut.done():
                fut.set_result(results)

    def _handle_async(self, rec: dict) -> None:
        cls = rec.get("class_", "")
        results = rec.get("results", {})

        if cls == "stopped":
            frame_data = results.get("frame", {})
            frame = self._parse_frame(frame_data)
            self.current_frame = frame
            self.on_stopped(frame)
            # Refresh breakpoints after stop
            asyncio.ensure_future(self._refresh_breakpoints())
        elif cls == "running":
            self.on_running()
        elif cls == "breakpoint-modified":
            bkpt = results.get("bkpt", {})
            if bkpt:
                self._update_breakpoint_from_mi(bkpt)
                self.on_breakpoints(list(self.breakpoints))
        elif cls == "breakpoint-deleted":
            num_str = results.get("id", "")
            try:
                num = int(num_str)
                self.breakpoints = [b for b in self.breakpoints if b.number != num]
                self.on_breakpoints(list(self.breakpoints))
            except (ValueError, TypeError):
                pass
        elif cls == "thread-group-started":
            pass

    # ------------------------------------------------------------------
    # MI command helpers
    # ------------------------------------------------------------------

    def _next_token(self) -> int:
        t = self._token
        self._token += 1
        return t

    def mi_command(self, cmd: str) -> None:
        """Send a raw MI command (without token)."""
        self.send_input(cmd + "\n")

    def cli_command(self, cmd: str) -> None:
        """Send a CLI command to GDB."""
        self.send_input(cmd + "\n")

    async def _refresh_breakpoints(self) -> None:
        self.mi_command("-break-list")

    def request_source_files(self) -> None:
        self.mi_command("-file-list-exec-source-files")

    def request_source_file(self) -> None:
        """Query the current source file (used at startup to pre-load it)."""
        self.mi_command("-file-list-exec-source-file")

    def set_breakpoint(self, location: str, temporary: bool = False) -> None:
        flag = "-t " if temporary else ""
        self.mi_command(f"-break-insert {flag}{location}")
        # Refresh after insert
        asyncio.ensure_future(self._do_refresh_breakpoints())

    async def _do_refresh_breakpoints(self) -> None:
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

    def _parse_frame(self, data: dict) -> Frame:
        if not isinstance(data, dict):
            return Frame()
        return Frame(
            file=data.get("file", ""),
            fullname=data.get("fullname", ""),
            line=self._safe_int(data.get("line", 0)),
            func=data.get("func", ""),
            addr=data.get("addr", ""),
        )

    def _update_breakpoint_from_mi(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        num = self._safe_int(data.get("number", 0))
        if num == 0:
            return
        # Find existing or create
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

    def _handle_source_files(self, files) -> None:
        if isinstance(files, list):
            paths: list[str] = []
            for f in files:
                if isinstance(f, dict):
                    p = f.get("fullname") or f.get("file", "")
                    if p:
                        paths.append(p)
                elif isinstance(f, str):
                    paths.append(f)
            self.source_files = sorted(set(paths))
            self.on_source_files(list(self.source_files))

    @staticmethod
    def _safe_int(val) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    def handle_breaklist_result(self, results: dict) -> None:
        """Process -break-list results."""
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
                bp = Breakpoint(
                    number=num,
                    file=bkpt_data.get("file", ""),
                    fullname=bkpt_data.get("fullname", ""),
                    line=self._safe_int(bkpt_data.get("line", 0)),
                    addr=bkpt_data.get("addr", ""),
                    enabled=bkpt_data.get("enabled", "y") == "y",
                    temporary=bkpt_data.get("disp", "") == "del",
                )
                new_bps.append(bp)
        self.breakpoints = new_bps
        self.on_breakpoints(list(self.breakpoints))

    def terminate(self) -> None:
        """Kill the GDB process."""
        if self._proc and self._proc.isalive():
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
