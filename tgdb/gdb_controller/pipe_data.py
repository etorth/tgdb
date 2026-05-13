"""Unified pipe reader for ``GDBController``.

Provides ``PipeDataMixin``, which reads binary frames from the single
GDB→tgdb pipe.  The pipe carries both lightweight events (before-prompt,
register-changed, objfile, inferior-call, gdb-exiting) and bulk data
payloads (locals, stack, threads, registers) in one stream.

Frame format
~~~~~~~~~~~~
``[1-byte tag][4-byte BE payload_length][payload]``

Lightweight event tags (payload_length = 0):
  ``P`` — before_prompt
  ``O`` — new_objfile
  ``F`` — free_objfile
  ``C`` — clear_objfiles
  ``X`` — gdb_exiting

Event tags with small payload:
  ``R`` — register_changed (4-byte signed BE regnum, -1 = all)
  ``I`` — inferior_call (1 byte: 0x00 = pre, 0x01 = post)

Bulk data tags (payload = zlib-compressed JSON):
  ``l`` — local variables
  ``s`` — stack frames
  ``t`` — thread info (dict with ``threads`` list + ``current-thread-id``)
  ``r`` — register values
"""

import asyncio
import json
import logging
import os
import struct
import zlib

from .types import (
    Frame,
    LocalVariable,
    RegisterInfo,
    ThreadInfo,
    normalize_addr,
)

_log = logging.getLogger("tgdb.gdb_controller")

# Maximum buffer size for incoming pipe data.  16 MB is generous;
# individual frames should rarely exceed a few hundred KB even for
# programs with thousands of threads and locals.
_PIPE_BUF_MAX_BYTES = 16 * 1024 * 1024

# Tags that carry no payload (5 bytes total: tag + 4 zero bytes).
_ZERO_PAYLOAD_TAGS = frozenset(b"POFCX")

# Tags with fixed-size payloads.
_FIXED_PAYLOAD = {
    ord(b"R"): 4,   # 4-byte signed BE regnum
    ord(b"I"): 1,   # 1-byte pre/post flag
}

# Tags that carry variable-length zlib-compressed JSON payloads.
_DATA_TAGS = frozenset(b"lstr")


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

        Frame format: ``[1-byte tag][4-byte BE payload_length][payload]``.
        """
        prompt_pending = False
        objfiles_pending = False
        pending_regnums: set[int] = set()

        while len(self._pipe_buf) >= 5:
            tag_byte = self._pipe_buf[0]
            payload_len = struct.unpack_from(">I", self._pipe_buf, 1)[0]
            total = 5 + payload_len
            if payload_len > _PIPE_BUF_MAX_BYTES:
                _log.warning(f"pipe: invalid payload size {payload_len}; resetting")
                self._pipe_buf = b""
                return

            if len(self._pipe_buf) < total:
                break

            payload = self._pipe_buf[5:total]
            self._pipe_buf = self._pipe_buf[total:]

            if tag_byte in _ZERO_PAYLOAD_TAGS:
                if tag_byte == ord(b"P"):
                    prompt_pending = True
                elif tag_byte in (ord(b"O"), ord(b"F"), ord(b"C")):
                    objfiles_pending = True
                elif tag_byte == ord(b"X"):
                    try:
                        self.on_gdb_exiting()
                    except Exception:
                        _log.debug("gdb_exiting callback raised", exc_info=True)
            elif tag_byte == ord(b"R"):
                if len(payload) >= 4:
                    regnum = struct.unpack(">i", payload[:4])[0]
                    if regnum < 0:
                        regnum = -1
                    pending_regnums.add(regnum)
            elif tag_byte == ord(b"I"):
                try:
                    if payload and payload[0] == 0:
                        self.on_inferior_call_pre()
                    else:
                        self.on_inferior_call_post()
                except Exception:
                    _log.debug("inferior_call callback raised", exc_info=True)
            elif tag_byte in _DATA_TAGS:
                try:
                    json_bytes = zlib.decompress(payload)
                    data = json.loads(json_bytes)
                except Exception as exc:
                    _log.warning(f"pipe: frame decode failed (tag={chr(tag_byte)!r}): {exc!r}")
                    continue
                self._dispatch_pipe_data(tag_byte, data)
            else:
                _log.debug(f"pipe: unknown tag 0x{tag_byte:02x}")

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
        elif tag_byte == ord(b"t"):
            self._handle_pipe_threads(data)
        elif tag_byte == ord(b"r"):
            self._handle_pipe_registers(data)
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


    def _handle_pipe_threads(self, data: dict) -> None:
        """Build ``ThreadInfo`` list from pipe JSON and fire ``on_threads``."""
        if self._inferior_running:
            return
        if not isinstance(data, dict):
            return

        current_thread_id = data.get("current-thread-id", "")
        if isinstance(current_thread_id, str) and current_thread_id:
            self.current_thread_id = current_thread_id

        raw_threads = data.get("threads", [])
        if not isinstance(raw_threads, list):
            return

        threads = []
        for raw in raw_threads:
            if not isinstance(raw, dict):
                continue
            frame_data = raw.get("frame")
            if isinstance(frame_data, dict):
                parsed_frame = Frame(
                    level=int(frame_data.get("level", 0)),
                    file=frame_data.get("file", ""),
                    fullname=frame_data.get("fullname", ""),
                    line=int(frame_data.get("line", 0)),
                    func=frame_data.get("func", ""),
                    addr=frame_data.get("addr", ""),
                )
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
        _log.debug(f"pipe threads: {len(threads)} threads")
        self.threads = threads
        self._emit_threads()


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
