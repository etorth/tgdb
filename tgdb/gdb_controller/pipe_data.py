"""Pipe-based data handlers for ``GDBController``.

Provides ``PipeDataMixin``, which processes length-prefixed binary frames
from the GDB→tgdb data pipe.  Each frame carries zlib-compressed JSON for
one of the heavy data payloads (locals, stack, threads, registers) that
used to travel over the MI channel.

Frame format
~~~~~~~~~~~~
``[4-byte BE length][1-byte tag][zlib-compressed JSON]``

Tags:
  ``L`` — local variables
  ``S`` — stack frames
  ``T`` — thread info (dict with ``threads`` list + ``current-thread-id``)
  ``R`` — register values
"""

import asyncio
import json
import logging
import os
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
_DATA_BUF_MAX_BYTES = 16 * 1024 * 1024


class PipeDataMixin:
    """Mixin providing data-pipe frame parsing and dispatch.

    Expects the host class to set ``self._data_pipe_r`` (read fd) and
    ``self._data_buf`` (bytes buffer) before the asyncio reader is
    registered.  The host also provides the standard controller callbacks
    (``on_locals``, ``on_stack``, ``on_threads``, ``on_registers``) and
    state attributes (``_inferior_running``, ``current_thread_id``, etc.).
    """

    def _on_data_pipe_readable(self) -> None:
        """Drain the data pipe and process complete frames."""
        try:
            while True:
                chunk = os.read(self._data_pipe_r, 65536)
                if not chunk:
                    self._unregister_data_pipe()
                    return
                self._data_buf += chunk
                if len(chunk) < 65536:
                    break
        except BlockingIOError:
            pass
        except OSError as exc:
            _log.warning(f"data pipe read failed: {exc!r}")
            self._unregister_data_pipe()
            return

        if len(self._data_buf) > _DATA_BUF_MAX_BYTES:
            _log.warning(
                f"data pipe buffer exceeded {_DATA_BUF_MAX_BYTES} bytes; resetting"
            )
            self._data_buf = b""
            return

        self._process_data_frames()


    def _unregister_data_pipe(self) -> None:
        """Remove the data-pipe asyncio reader and discard buffer."""
        try:
            asyncio.get_running_loop().remove_reader(self._data_pipe_r)
        except Exception:
            pass
        self._data_buf = b""


    def _process_data_frames(self) -> None:
        """Parse and dispatch complete length-prefixed frames."""
        while len(self._data_buf) >= 5:
            frame_size = int.from_bytes(self._data_buf[:4], "big")
            if frame_size < 1 or frame_size > _DATA_BUF_MAX_BYTES:
                _log.warning(f"data pipe: invalid frame size {frame_size}; resetting")
                self._data_buf = b""
                return

            total = 4 + frame_size
            if len(self._data_buf) < total:
                break

            tag = self._data_buf[4:5]
            compressed = self._data_buf[5:total]
            self._data_buf = self._data_buf[total:]

            try:
                json_bytes = zlib.decompress(compressed)
                data = json.loads(json_bytes)
            except Exception as exc:
                _log.warning(f"data pipe: frame decode failed: {exc!r}")
                continue

            self._dispatch_pipe_data(tag, data)


    def _dispatch_pipe_data(self, tag: bytes, data) -> None:
        """Route a decoded pipe frame to the appropriate handler."""
        if tag == b"L":
            self._handle_pipe_locals(data)
        elif tag == b"S":
            self._handle_pipe_stack(data)
        elif tag == b"T":
            self._handle_pipe_threads(data)
        elif tag == b"R":
            self._handle_pipe_registers(data)
        else:
            _log.debug(f"data pipe: unknown tag {tag!r}")


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
