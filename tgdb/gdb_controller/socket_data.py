"""Unified socket reader for ``GDBController``.

Provides ``SocketDataMixin``, which reads binary frames from the
``AF_UNIX`` socketpair shared between tgdb and GDB.  The socket
carries both lightweight events (register-changed, objfile,
inferior-call, gdb-exiting) and bulk data payloads (locals,
stack, threads, registers, frame info, breakpoints) in one stream.
The socket is bidirectional: GDB writes data/events to tgdb, and tgdb
can write cancel tokens back to GDB.

Frame format
~~~~~~~~~~~~
The tag byte alone determines the frame structure:

No-payload tags (1 byte total):
  ``O`` — new_objfile
  ``F`` — free_objfile
  ``C`` — clear_objfiles
  ``X`` — gdb_exiting

Fixed-payload tags (tag + fixed-size payload, no length field):
  ``I`` — inferior_call (1 byte: 0x00 = pre, 0x01 = post)

Varint-payload tags (tag + unsigned varint):
  ``R`` — register_changed (regnum + 1 as unsigned varint; 0 = all)

Variable-length tags (``[tag 1B][ctl 1B][length varint][payload]``):
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
import zlib

from ..async_util import spawn_eager_task
from .errors import GDBRequestCancelled, GDBRequestTimeout
from .types import (
    Breakpoint,
    Frame,
    LocalVariable,
    RegisterInfo,
    normalize_addr,
)

_log = logging.getLogger("tgdb.gdb_controller")


# Maximum buffer size for incoming socket data.  16 MB is generous;
# individual frames should rarely exceed a few hundred KB even for
# programs with thousands of threads and locals.
_SOCK_BUF_MAX_BYTES = 16 * 1024 * 1024

# Tags that carry no payload (1 byte total — tag only).
_ZERO_PAYLOAD_TAGS = frozenset(b"OFCX")

# Tags with fixed-size payloads (tag + payload, no length field).
_FIXED_PAYLOAD = {
    ord(b"I"): 1,   # 1-byte pre/post flag
}

# Tags whose payload is a single zigzag-encoded varint.
_VARINT_TAGS = frozenset({ord(b"R")})

# Tags that carry variable-length payloads.
# Frame: [1-byte tag][1-byte control][length varint][payload]
# Control byte bit 0 (_CTL_COMPRESSED): payload is zlib-compressed.
_VAR_LEN_TAGS = frozenset(b"lsrbfD")
_CTL_COMPRESSED = 0x01

# Data tags that embed a varint MI token at the start of their payload.
# Log messages (``D``) do not carry a token.
_DATA_TAGS = frozenset(b"lsrbf")


# ---------------------------------------------------------------------------
# Varint helpers — unsigned LEB128
# ---------------------------------------------------------------------------

def _decode_varint(buf, offset=0):
    """Decode one unsigned LEB128 varint from *buf* starting at *offset*.

    Returns ``(value, new_offset)`` on success, or ``(None, offset)``
    if the buffer is incomplete (not enough bytes yet).
    """
    result = 0
    shift = 0
    start = offset
    while offset < len(buf):
        byte = buf[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            return result, offset
        shift += 7
    return None, start


class SocketDataMixin:
    """Mixin providing unified socket frame parsing and dispatch.

    Expects the host class to set ``self._sock_tgdb`` (local fd) and
    ``self._sock_buf`` (bytes buffer) before the asyncio reader is
    registered.  The host also provides the standard controller callbacks
    (``on_locals``, ``on_stack``, ``on_threads``, ``on_registers``,
    ``on_register_changed``, ``on_objfiles_changed``,
    ``on_inferior_call_pre``, ``on_inferior_call_post``, ``on_gdb_exiting``)
    and state attributes (``_inferior_running``, ``current_thread_id``, etc.).
    """

    def _on_sock_readable(self) -> None:
        """Drain the socket and spawn tasks for complete frames."""
        try:
            while True:
                chunk = os.read(self._sock_tgdb, 65536)
                if not chunk:
                    self._unregister_sock()
                    return
                self._sock_buf += chunk
                if len(chunk) < 65536:
                    break
        except BlockingIOError:
            pass
        except OSError as exc:
            _log.warning(f"socket read failed: {exc!r}")
            self._unregister_sock()
            return

        if len(self._sock_buf) > _SOCK_BUF_MAX_BYTES:
            _log.warning(
                f"socket buffer exceeded {_SOCK_BUF_MAX_BYTES} bytes; resetting"
            )
            self._sock_buf = b""
            return

        self._spawn_sock_frame_tasks()


    def _unregister_sock(self) -> None:
        """Remove the socket asyncio reader, discard buffer."""
        try:
            asyncio.get_running_loop().remove_reader(self._sock_tgdb)
        except Exception:
            pass
        self._sock_buf = b""


    def _spawn_sock_frame_tasks(self) -> None:
        """Extract complete socket frames and spawn one eager task per frame."""
        while self._sock_buf:
            frame_data = self._extract_one_sock_frame()
            if frame_data is None:
                break
            spawn_eager_task(
                self._process_sock_frame(frame_data),
                self._io_tasks,
                name="sock-process",
            )


    def _extract_one_sock_frame(self) -> tuple | None:
        """Extract one complete frame from ``_sock_buf``.

        Returns a tuple describing the frame, or ``None`` if the buffer
        does not contain a complete frame yet.  Advances ``_sock_buf``
        past the consumed bytes on success.

        Return shapes by tag type:

        - Zero-payload: ``(tag_byte,)``
        - Fixed-payload: ``(tag_byte, payload_bytes)``
        - Varint: ``(tag_byte, decoded_int)``
        - Variable-length: ``(tag_byte, ctl_byte, raw_payload)``
        - Unknown tag: ``(tag_byte,)`` (one byte consumed)
        """
        if not self._sock_buf:
            return None
        tag_byte = self._sock_buf[0]

        if tag_byte in _ZERO_PAYLOAD_TAGS:
            self._sock_buf = self._sock_buf[1:]
            return (tag_byte,)

        if tag_byte in _FIXED_PAYLOAD:
            fixed_size = _FIXED_PAYLOAD[tag_byte]
            total = 1 + fixed_size
            if len(self._sock_buf) < total:
                return None
            payload = self._sock_buf[1:total]
            self._sock_buf = self._sock_buf[total:]
            return (tag_byte, payload)

        if tag_byte in _VARINT_TAGS:
            raw, after = _decode_varint(self._sock_buf, 1)
            if raw is None:
                return None
            self._sock_buf = self._sock_buf[after:]
            return (tag_byte, raw)

        if tag_byte in _VAR_LEN_TAGS:
            if len(self._sock_buf) < 3:
                return None
            payload_len, after = _decode_varint(self._sock_buf, 2)
            if payload_len is None:
                return None
            if payload_len > _SOCK_BUF_MAX_BYTES:
                _log.warning(f"socket: invalid payload size {payload_len}; resetting")
                self._sock_buf = b""
                return None
            total = after + payload_len
            if len(self._sock_buf) < total:
                return None
            ctl = self._sock_buf[1]
            payload = self._sock_buf[after:total]
            self._sock_buf = self._sock_buf[total:]
            return (tag_byte, ctl, payload)

        # Unknown tag — skip one byte.
        _log.debug(f"socket: unknown tag 0x{tag_byte:02x}")
        self._sock_buf = self._sock_buf[1:]
        return (tag_byte,)


    async def _process_sock_frame(self, frame: tuple) -> None:
        """Handle a single extracted socket frame.

        Most tags complete synchronously (no real await); only the
        ``f`` (frame-info) tag triggers follow-up MI round-trips.
        """
        tag_byte = frame[0]

        if tag_byte in _ZERO_PAYLOAD_TAGS:
            if tag_byte in (ord(b"O"), ord(b"F"), ord(b"C")):
                try:
                    self.on_objfiles_changed()
                except Exception:
                    _log.debug("on_objfiles_changed callback raised", exc_info=True)
            elif tag_byte == ord(b"X"):
                try:
                    self.on_gdb_exiting()
                except Exception:
                    _log.debug("gdb_exiting callback raised", exc_info=True)

        elif tag_byte in _FIXED_PAYLOAD:
            payload = frame[1]
            if tag_byte == ord(b"I"):
                try:
                    if payload[0] == 0:
                        self.on_inferior_call_pre()
                    else:
                        self.on_inferior_call_post()
                except Exception:
                    _log.debug("inferior_call callback raised", exc_info=True)

        elif tag_byte in _VARINT_TAGS:
            raw = frame[1]
            if tag_byte == ord(b"R"):
                regnum = raw - 1
                if regnum < 0:
                    regnum = -1
                try:
                    self.on_register_changed(regnum)
                except Exception:
                    _log.debug("on_register_changed callback raised", exc_info=True)

        elif tag_byte in _VAR_LEN_TAGS:
            ctl = frame[1]
            payload = frame[2]

            if ctl & _CTL_COMPRESSED:
                try:
                    payload = zlib.decompress(payload)
                except Exception as exc:
                    _log.warning(f"socket: decompress failed (tag={chr(tag_byte)!r}): {exc!r}")
                    return

            if tag_byte == ord(b"D"):
                try:
                    msg = payload.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    msg = "<decode error>"
                _log.debug(f"SOCK<- gdb-python: {msg}")
            elif tag_byte in _DATA_TAGS:
                mi_token, json_start = _decode_varint(payload, 0)
                if mi_token is None:
                    _log.warning(f"socket: missing MI token (tag={chr(tag_byte)!r})")
                    return
                try:
                    data = json.loads(payload[json_start:])
                except Exception as exc:
                    _log.warning(f"socket: JSON decode failed (tag={chr(tag_byte)!r}): {exc!r}")
                    return
                self._try_resolve_sock_pending(mi_token, tag_byte, data)
                await self._dispatch_sock_data(tag_byte, data)
            else:
                try:
                    data = json.loads(payload)
                except Exception as exc:
                    _log.warning(f"socket: JSON decode failed (tag={chr(tag_byte)!r}): {exc!r}")
                    return
                await self._dispatch_sock_data(tag_byte, data)

    async def _dispatch_sock_data(self, tag_byte: int, data) -> None:
        """Route a decoded socket data frame to the appropriate handler."""
        if tag_byte == ord(b"l"):
            self._handle_sock_locals(data)
        elif tag_byte == ord(b"s"):
            self._handle_sock_stack(data)
        elif tag_byte == ord(b"r"):
            self._handle_sock_registers(data)
        elif tag_byte == ord(b"f"):
            await self._handle_sock_frame_info(data)
        elif tag_byte == ord(b"b"):
            self._handle_sock_breakpoints(data)
        else:
            _log.debug(f"socket: unknown data tag {chr(tag_byte)!r}")


    def _try_resolve_sock_pending(self, mi_token: int, tag_byte: int, data) -> None:
        """Correlate socket data with its MI token for two-part completion.

        Sets ``socket_response`` on the ``PendingEntry``.  If the MI
        ``^done`` response already arrived (``entry.mi_response`` is set),
        resolves the Future with the socket data.  Otherwise the entry
        waits for ``_handle_result`` to complete the second half.

        A zero *mi_token* means the GDB side sent no token — skip
        correlation entirely.
        """
        if mi_token == 0:
            return

        entry = self._pending.get(mi_token)
        if entry is None:
            # Token already removed (timeout / cancel / cleanup).
            _log.debug(f"two-part dropped (no entry): token={mi_token}")
            return

        entry.socket_response = data

        if entry.mi_response is not None:
            # MI already arrived with "done" — resolve now.
            self._pending.pop(mi_token, None)
            self._request_meta.pop(mi_token, None)
            if not entry.future.done():
                entry.future.set_result(data)
            _log.debug(f"two-part resolved (sock-second): token={mi_token}")
        else:
            _log.debug(f"two-part stashed (sock-first): token={mi_token}")


    def _handle_sock_locals(self, data: list) -> None:
        """Build ``LocalVariable`` list from socket JSON and fire ``on_locals``."""
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
        _log.debug(f"socket locals: {len(variables)} variables")
        self.locals = variables
        self.on_locals(list(variables))


    def _handle_sock_stack(self, data: list) -> None:
        """Build ``Frame`` list from socket JSON and fire ``on_stack``."""
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
        _log.debug(f"socket stack: {len(frames)} frames")
        self.stack = frames
        self.on_stack(list(frames))


    def _handle_sock_registers(self, data: list) -> None:
        """Build ``RegisterInfo`` list from socket JSON and fire ``on_registers``."""
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

        _log.debug(f"socket registers: {len(registers)} registers")
        self.register_names = names
        self._register_values = values
        self.registers = registers
        self.on_registers(list(registers))


    async def _handle_sock_frame_info(self, data: dict) -> None:
        """Parse frame info from socket JSON and drive the frame-changed chain.

        Called when the ``f`` data tag arrives in response to an
        ``request_current_location`` MI command (used only during
        startup).  Sets ``current_frame``, fires ``on_frame_changed``
        / ``on_source_file``, and awaits locals/stack/threads/registers
        collection sequentially.  Skips data collection when the parsed
        frame is identical to the already-active ``current_frame``.
        """
        if self._inferior_running:
            return
        if not isinstance(data, dict) or not data:
            self.request_source_file(report_error=False)
            return

        parsed = Frame(
            level=self._safe_int(data.get("level", 0)),
            file=data.get("file", ""),
            fullname=data.get("fullname", ""),
            line=self._safe_int(data.get("line", 0)),
            func=data.get("func", ""),
            addr=data.get("addr", ""),
        )
        _log.debug(f"socket frame: {parsed.func} {parsed.file}:{parsed.line}")

        if self.current_frame == parsed:
            return

        self.current_frame = parsed
        path = parsed.fullname or parsed.file
        self.on_frame_changed(parsed)
        if path:
            self.on_source_file(path, parsed.line)
        else:
            self.request_source_file(report_error=False)

        try:
            await self.request_current_frame_locals(report_error=False)
            await self.request_current_stack_frames(report_error=False)
            await self.request_current_threads(report_error=False)
            await self.request_current_registers(report_error=False)
        except (GDBRequestCancelled, GDBRequestTimeout):
            _log.debug("startup data collection cancelled")


    def _handle_sock_breakpoints(self, data: list) -> None:
        """Parse breakpoint list from socket JSON and fire ``on_breakpoints``."""
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
        _log.info(f"socket breaklist: {len(new_bps)} breakpoints")
        self.breakpoints = new_bps
        self.on_breakpoints(list(self.breakpoints))
