"""Unified pipe reader for ``GDBController``.

Provides ``PipeDataMixin``, which reads binary frames from the single
GDB→tgdb pipe.  The pipe carries both lightweight events (before-prompt,
register-changed, objfile, inferior-call, gdb-exiting) and bulk data
payloads (locals, stack, threads, registers, frame info, breakpoints)
in one stream.

Frame format
~~~~~~~~~~~~
The tag byte alone determines the frame structure:

No-payload tags (1 byte total):
  ``P`` — before_prompt
  ``O`` — new_objfile
  ``F`` — free_objfile
  ``C`` — clear_objfiles
  ``X`` — gdb_exiting

Fixed-payload tags (tag + fixed-size payload, no length field):
  ``R`` — register_changed (4-byte signed BE regnum, -1 = all)
  ``I`` — inferior_call (1 byte: 0x00 = pre, 0x01 = post)

Variable-length tags (``[tag 1B][ctl 1B][length 7B BE][payload]``):
  ``l`` — local variables (JSON, auto-compressed)
  ``s`` — stack frames (JSON, auto-compressed)
  ``r`` — register values (JSON, auto-compressed)
  ``f`` — current frame info (JSON, auto-compressed)
  ``b`` — breakpoint list (JSON, auto-compressed)
  ``D`` — diagnostic log message (raw UTF-8)

The control byte carries bit flags:
  bit 0 (0x01) — payload is zlib-compressed
"""

import asyncio
import json
import logging
import os
import struct
import zlib

from ..async_util import supervise
from .types import (
    Breakpoint,
    Frame,
    LocalVariable,
    RegisterInfo,
    normalize_addr,
)

_log = logging.getLogger("tgdb.gdb_controller")

# Maximum buffer size for incoming pipe data.  16 MB is generous;
# individual frames should rarely exceed a few hundred KB even for
# programs with thousands of threads and locals.
_PIPE_BUF_MAX_BYTES = 16 * 1024 * 1024

# Tags that carry no payload (1 byte total — tag only).
_ZERO_PAYLOAD_TAGS = frozenset(b"POFCX")

# Tags with fixed-size payloads (tag + payload, no length field).
_FIXED_PAYLOAD = {
    ord(b"R"): 4,   # 4-byte signed BE regnum
    ord(b"I"): 1,   # 1-byte pre/post flag
}

# Tags that carry variable-length payloads.
# Frame: [1-byte tag][1-byte control][7-byte BE payload_length][payload]
# Control byte bit 0 (_CTL_COMPRESSED): payload is zlib-compressed.
_VAR_LEN_TAGS = frozenset(b"lsrbfD")
_CTL_COMPRESSED = 0x01


class PipeDataMixin:
    """Mixin providing unified pipe frame parsing and dispatch.

    Expects the host class to set ``self._pipe_r`` (read fd) and
    ``self._pipe_buf`` (bytes buffer) before the asyncio reader is
    registered.  The host also provides the standard controller callbacks
    (``on_locals``, ``on_stack``, ``on_threads``, ``on_registers``,
    ``on_cli_prompt``, ``on_register_changed``, ``on_objfiles_changed``,
    ``on_inferior_call_pre``, ``on_inferior_call_post``, ``on_gdb_exiting``)
    and state attributes (``_inferior_running``, ``current_thread_id``, etc.).
    """

    def _on_pipe_readable(self) -> None:
        """Drain the pipe and process complete frames."""
        try:
            while True:
                chunk = os.read(self._pipe_r, 65536)
                if not chunk:
                    self._unregister_pipe()
                    return
                self._pipe_buf += chunk
                if len(chunk) < 65536:
                    break
        except BlockingIOError:
            pass
        except OSError as exc:
            _log.warning(f"pipe read failed: {exc!r}")
            self._unregister_pipe()
            return

        if len(self._pipe_buf) > _PIPE_BUF_MAX_BYTES:
            _log.warning(
                f"pipe buffer exceeded {_PIPE_BUF_MAX_BYTES} bytes; resetting"
            )
            self._pipe_buf = b""
            return

        self._process_pipe_frames()


    def _unregister_pipe(self) -> None:
        """Remove the pipe asyncio reader and discard buffer."""
        try:
            asyncio.get_running_loop().remove_reader(self._pipe_r)
        except Exception:
            pass
        self._pipe_buf = b""


    def _process_pipe_frames(self) -> None:
        """Parse and dispatch complete frames from the unified pipe.

        The tag byte determines the frame structure:
        - No-payload tags: 1 byte total (tag only).
        - Fixed-payload tags: 1 + N bytes (tag + known payload size).
        - Data tags: 1 + 8 + payload_length bytes (tag + 8-byte BE length + payload).
        """
        prompt_pending = False
        objfiles_pending = False
        pending_regnums: set[int] = set()

        while self._pipe_buf:
            tag_byte = self._pipe_buf[0]

            if tag_byte in _ZERO_PAYLOAD_TAGS:
                self._pipe_buf = self._pipe_buf[1:]
                if tag_byte == ord(b"P"):
                    prompt_pending = True
                elif tag_byte in (ord(b"O"), ord(b"F"), ord(b"C")):
                    objfiles_pending = True
                elif tag_byte == ord(b"X"):
                    try:
                        self.on_gdb_exiting()
                    except Exception:
                        _log.debug("gdb_exiting callback raised", exc_info=True)

            elif tag_byte in _FIXED_PAYLOAD:
                fixed_size = _FIXED_PAYLOAD[tag_byte]
                total = 1 + fixed_size
                if len(self._pipe_buf) < total:
                    break
                payload = self._pipe_buf[1:total]
                self._pipe_buf = self._pipe_buf[total:]

                if tag_byte == ord(b"R"):
                    regnum = struct.unpack(">i", payload[:4])[0]
                    if regnum < 0:
                        regnum = -1
                    pending_regnums.add(regnum)
                elif tag_byte == ord(b"I"):
                    try:
                        if payload[0] == 0:
                            self.on_inferior_call_pre()
                        else:
                            self.on_inferior_call_post()
                    except Exception:
                        _log.debug("inferior_call callback raised", exc_info=True)

            elif tag_byte in _VAR_LEN_TAGS:
                if len(self._pipe_buf) < 9:
                    break
                ctl = self._pipe_buf[1]
                payload_len = struct.unpack(">Q", b"\x00" + self._pipe_buf[2:9])[0]
                if payload_len > _PIPE_BUF_MAX_BYTES:
                    _log.warning(f"pipe: invalid payload size {payload_len}; resetting")
                    self._pipe_buf = b""
                    return
                total = 9 + payload_len
                if len(self._pipe_buf) < total:
                    break
                payload = self._pipe_buf[9:total]
                self._pipe_buf = self._pipe_buf[total:]

                if ctl & _CTL_COMPRESSED:
                    try:
                        payload = zlib.decompress(payload)
                    except Exception as exc:
                        _log.warning(f"pipe: decompress failed (tag={chr(tag_byte)!r}): {exc!r}")
                        continue

                if tag_byte == ord(b"D"):
                    try:
                        msg = payload.decode("utf-8", errors="replace").rstrip("\n")
                    except Exception:
                        msg = "<decode error>"
                    _log.debug(f"gdb-python: {msg}")
                else:
                    try:
                        data = json.loads(payload)
                    except Exception as exc:
                        _log.warning(f"pipe: JSON decode failed (tag={chr(tag_byte)!r}): {exc!r}")
                        continue
                    self._dispatch_pipe_data(tag_byte, data)

            else:
                _log.debug(f"pipe: unknown tag 0x{tag_byte:02x}")
                self._pipe_buf = self._pipe_buf[1:]

        # Coalesced event dispatch — at most one callback per tag per read.
        if objfiles_pending:
            try:
                self.on_objfiles_changed()
            except Exception:
                _log.debug("on_objfiles_changed callback raised", exc_info=True)

        if pending_regnums:
            if -1 in pending_regnums:
                pending_regnums = {-1}
            for regnum in pending_regnums:
                try:
                    self.on_register_changed(regnum)
                except Exception:
                    _log.debug("on_register_changed callback raised", exc_info=True)

        if prompt_pending:
            try:
                self.on_cli_prompt()
            except Exception:
                _log.debug("on_cli_prompt callback raised", exc_info=True)


    def _dispatch_pipe_data(self, tag_byte: int, data) -> None:
        """Route a decoded pipe data frame to the appropriate handler."""
        if tag_byte == ord(b"l"):
            self._handle_pipe_locals(data)
        elif tag_byte == ord(b"s"):
            self._handle_pipe_stack(data)
        elif tag_byte == ord(b"r"):
            self._handle_pipe_registers(data)
        elif tag_byte == ord(b"f"):
            self._handle_pipe_frame_info(data)
        elif tag_byte == ord(b"b"):
            self._handle_pipe_breakpoints(data)
        else:
            _log.debug(f"pipe: unknown data tag {chr(tag_byte)!r}")


    def _handle_pipe_locals(self, data: list) -> None:
        """Build ``LocalVariable`` list from pipe JSON and fire ``on_locals``."""
        if self._inferior_running:
            return
        if not isinstance(data, list):
            return

        variables = [
            LocalVariable(
                name=d.get("name", ""),
                value=d.get("value", ""),
                type=d.get("type", ""),
                is_arg=bool(d.get("is_arg", False)),
                addr=normalize_addr(d.get("addr", "")),
                is_shadowed=bool(d.get("is_shadowed", False)),
                is_reference=bool(d.get("is_reference", False)),
                line=int(d.get("line", 0)),
                depth=int(d.get("depth", 0)),
            )
            for d in data
            if d.get("name")
        ]
        _log.debug(f"pipe locals: {len(variables)} variables")
        self.locals = variables
        self.on_locals(list(variables))


    def _handle_pipe_stack(self, data: list) -> None:
        """Build ``Frame`` list from pipe JSON and fire ``on_stack``."""
        if self._inferior_running:
            return
        if not isinstance(data, list):
            return

        frames = []
        for d in data:
            if not isinstance(d, dict):
                continue
            frames.append(
                Frame(
                    level=int(d.get("level", 0)),
                    file=d.get("file", ""),
                    fullname=d.get("fullname", ""),
                    line=int(d.get("line", 0)),
                    func=d.get("func", ""),
                    addr=d.get("addr", ""),
                )
            )
        _log.debug(f"pipe stack: {len(frames)} frames")
        self.stack = frames
        self.on_stack(list(frames))


    def _handle_pipe_registers(self, data: list) -> None:
        """Build ``RegisterInfo`` list from pipe JSON and fire ``on_registers``."""
        if self._inferior_running:
            return
        if not isinstance(data, list):
            return

        registers = []
        names: list[str] = []
        values: dict[int, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            value = item.get("value", "")
            number = int(item.get("number", 0))
            if not name:
                continue
            registers.append(RegisterInfo(number=number, name=name, value=value))
            while len(names) <= number:
                names.append("")
            names[number] = name
            values[number] = value

        _log.debug(f"pipe registers: {len(registers)} registers")
        self.register_names = names
        self._register_values = values
        self.registers = registers
        self.on_registers(list(registers))


    def _handle_pipe_frame_info(self, data: dict) -> None:
        """Parse frame info from pipe JSON and drive the frame-changed chain.

        Mirrors ``_handle_frame_result`` in ``results.py``: sets
        ``current_frame``, fires ``on_frame_changed`` / ``on_source_file``,
        and kicks off locals/stack/threads/registers collection.

        Skips data collection when the parsed frame is identical to the
        already-active ``current_frame``.  This breaks the feedback loop
        where each MI command's ``before_prompt`` event triggers another
        ``request_current_location`` — the second (and all subsequent)
        frame-info responses report the same frame and are no-ops.

        When the frame is unchanged, ``_frame_request_inflight`` is left
        True so that prompts from the in-flight MI commands spawned by
        the *previous* (genuine) frame change cannot re-trigger this
        cycle.  The flag is only cleared when a genuinely new frame
        arrives, at which point a fresh round of data collection begins.
        """
        if self._inferior_running:
            self._frame_request_inflight = False
            return
        if not isinstance(data, dict) or not data:
            self._frame_request_inflight = False
            self.request_source_file(report_error=False)
            return

        parsed = Frame(
            level=int(data.get("level", 0)),
            file=data.get("file", ""),
            fullname=data.get("fullname", ""),
            line=int(data.get("line", 0)),
            func=data.get("func", ""),
            addr=data.get("addr", ""),
        )
        _log.debug(f"pipe frame: {parsed.func} {parsed.file}:{parsed.line}")

        if self.current_frame == parsed:
            return

        self._frame_request_inflight = False
        self.current_frame = parsed
        path = parsed.fullname or parsed.file
        self.on_frame_changed(parsed)
        if path:
            self.on_source_file(path, parsed.line)
        else:
            self.request_source_file(report_error=False)

        supervise(self.request_current_frame_locals(report_error=False), name="pipe-frame-locals")
        supervise(self.request_current_stack_frames(report_error=False), name="pipe-frame-stack")
        supervise(self.request_current_threads(report_error=False), name="pipe-frame-threads")
        supervise(self.request_current_registers(report_error=False), name="pipe-frame-registers")


    def _handle_pipe_breakpoints(self, data: list) -> None:
        """Parse breakpoint list from pipe JSON and fire ``on_breakpoints``."""
        if not isinstance(data, list):
            return

        new_bps: list[Breakpoint] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            num = int(raw.get("number", 0))
            if num:
                new_bps.append(
                    Breakpoint(
                        number=num,
                        file=raw.get("file", ""),
                        fullname=raw.get("fullname", ""),
                        line=int(raw.get("line", 0)),
                        addr=raw.get("addr", ""),
                        enabled=bool(raw.get("enabled", True)),
                        temporary=bool(raw.get("temporary", False)),
                    )
                )
        _log.info(f"pipe breaklist: {len(new_bps)} breakpoints")
        self.breakpoints = new_bps
        self.on_breakpoints(list(self.breakpoints))
