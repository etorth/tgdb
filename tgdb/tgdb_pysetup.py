import gdb
import json
import os
import struct
import zlib

# Lift GDB's memory read limit so str(val) never fails for large variables.
try:
    gdb.execute("set max-value-size unlimited", to_string=True)
except gdb.error:
    pass


_pipe_fd = None
_event_handlers_connected = False


def register_pipe_fd(fd):
    """Wire GDB Python events and data collection to a single pipe.

    tgdb opens one inheritable pipe before forking GDB and passes the write
    end's fd number here.  All communication uses a uniform binary frame
    format so lightweight events and bulk data share the same channel:

    Frame format: ``[1-byte tag][4-byte BE payload_length][payload]``

    Lightweight events (payload_length = 0, 5 bytes total):
      ``P``  before_prompt — refresh selected frame in source pane.
      ``O``  new_objfile — a shared library was just loaded.
      ``F``  free_objfile — a shared library was unloaded.
      ``C``  clear_objfiles — program space was wiped (e.g. ``kill``).
      ``X``  gdb_exiting — GDB's main loop is tearing down.

    Events with small payload:
      ``R``  register_changed — 4-byte signed BE regnum (-1 = all).
      ``I``  inferior_call — 1 byte (0x00 = pre, 0x01 = post).

    Bulk data events (payload = zlib-compressed JSON):
      ``l``  local variables
      ``s``  stack frames
      ``t``  thread info
      ``r``  register values

    Calling this again with a different fd retargets the existing handlers.
    Handlers are connected to GDB's event registries exactly once per
    Python process so a re-call cannot accumulate duplicates.
    """
    global _pipe_fd, _event_handlers_connected
    _pipe_fd = fd
    if _event_handlers_connected:
        return
    _event_handlers_connected = True

    def _emit(tag_byte, payload=b""):
        active_fd = _pipe_fd
        if active_fd is None:
            return
        header = tag_byte + struct.pack(">I", len(payload)) + payload
        try:
            os.write(active_fd, header)
        except (BlockingIOError, OSError):
            pass

    def _on_before_prompt():
        _emit(b"P")

    def _on_register_changed(event):
        try:
            regnum = int(event.regnum)
        except (AttributeError, ValueError, TypeError):
            regnum = -1
        _emit(b"R", struct.pack(">i", regnum))

    def _on_new_objfile(_event):
        _emit(b"O")

    def _on_free_objfile(_event):
        _emit(b"F")

    def _on_clear_objfiles(_event):
        _emit(b"C")

    def _on_inferior_call(event):
        if isinstance(event, gdb.InferiorCallPreEvent):
            _emit(b"I", b"\x00")
        else:
            _emit(b"I", b"\x01")

    def _on_gdb_exiting(_event):
        _emit(b"X")

    gdb.events.before_prompt.connect(_on_before_prompt)
    _try_connect("register_changed", _on_register_changed)
    _try_connect("new_objfile", _on_new_objfile)
    _try_connect("free_objfile", _on_free_objfile)
    _try_connect("clear_objfiles", _on_clear_objfiles)
    _try_connect("inferior_call", _on_inferior_call)
    _try_connect("gdb_exiting", _on_gdb_exiting)


def _try_connect(event_name, handler):
    """Connect to a gdb.events.* registry that may not exist on older GDBs."""
    registry = getattr(gdb.events, event_name, None)
    if registry is None:
        return
    try:
        registry.connect(handler)
    except Exception:
        pass


def _send_pipe_payload(tag, data):
    """Serialize *data* as JSON, zlib-compress, and write a framed payload.

    Uses the same unified pipe as lightweight events.  *tag* must be a
    single lowercase ASCII character (``l``, ``s``, ``t``, or ``r``).
    Returns True on success, False if the pipe is not registered or the
    write fails.
    """
    fd = _pipe_fd
    if fd is None:
        return False
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(json_bytes)
    tag_byte = tag.encode("ascii")[:1] if isinstance(tag, str) else tag[:1]
    header = tag_byte + struct.pack(">I", len(compressed))
    buf = header + compressed
    try:
        written = 0
        while written < len(buf):
            n = os.write(fd, buf[written:])
            if n <= 0:
                return False
            written += n
        return True
    except OSError:
        return False


def _format_value(val):
    """Format a gdb.Value with unlimited elements per-call.

    Uses ``format_string(max_elements=0)`` when available (GDB 9.1+)
    to avoid contaminating global ``set print elements`` settings.
    Falls back to ``str(val)`` on older builds.
    """
    try:
        return val.format_string(max_elements=0)
    except (TypeError, AttributeError):
        return str(val)


def _is_builtin_local_name(name):
    """Return True for compiler-generated locals we should hide.

    C++ range-for lowering creates reserved implementation names such as
    ``__for_begin``, ``__for_end``, and ``__for_range``.  Those variables are
    noise in the locals pane, so filter the whole ``__for_`` family here before
    the payload leaves GDB.  Also hide the standalone ``_`` scratch variable,
    which is commonly used as an intentionally-ignored binding.
    """
    if not name:
        return False

    if name == "_":
        return True

    return name.startswith("__for_")


# ---------------------------------------------------------------------------
# Pipe-based collection functions
#
# Each function collects data using GDB's Python API, serializes it as
# JSON, zlib-compresses the bytes, and writes a length-prefixed frame to
# the data pipe.  The MI return value is a tiny "ok" string so the MI
# channel is never congested by the payload.
# ---------------------------------------------------------------------------


def _collect_locals():
    """Collect local variables and send via data pipe (tag ``L``)."""
    try:
        frame = gdb.selected_frame()
    except gdb.error:
        _send_pipe_payload("l", [])
        return "ok"

    try:
        block = frame.block()
    except (gdb.error, RuntimeError) as exc:
        if "Cannot locate block" in str(exc):
            _send_pipe_payload("l", [])
            return "ok"
        raise

    sal = frame.find_sal()
    current_line = sal.line

    depth = 0
    all_vars = []
    seen_names = set()

    while block:
        for symbol in block:
            if not (symbol.is_variable or symbol.is_argument):
                continue

            name = symbol.name
            if _is_builtin_local_name(name):
                continue

            decl_line = symbol.line
            if current_line > 0 and not symbol.is_argument:
                if decl_line > 0 and decl_line >= current_line:
                    continue

            type_obj = symbol.type.strip_typedefs()

            is_lref = type_obj.code == gdb.TYPE_CODE_REF
            is_rref = type_obj.code == gdb.TYPE_CODE_RVALUE_REF
            is_reference = is_lref or is_rref

            try:
                val = symbol.value(frame)
                val_str = _format_value(val)

                if is_reference:
                    try:
                        addr_str = str(val.referenced_value().address)
                    except Exception:
                        addr_str = "unknown (referenced target)"
                else:
                    addr_str = str(val.address) if val.address else "register"

            except Exception:
                val_str = "<optimized out>"
                addr_str = "unknown"

            is_shadowed = name in seen_names

            if is_lref:
                ref_kind = "lvalue (&)"
            elif is_rref:
                ref_kind = "rvalue (&&)"
            else:
                ref_kind = None

            all_vars.append({
                "name": name,
                "value": val_str,
                "type": str(symbol.type),
                "is_arg": symbol.is_argument,
                "is_reference": is_reference,
                "ref_kind": ref_kind,
                "line": decl_line,
                "addr": addr_str,
                "depth": depth,
                "is_shadowed": is_shadowed,
                "scope_start": hex(block.start),
            })

            seen_names.add(name)

        if block.superblock is None:
            break

        if block.function is not None:
            break

        block = block.superblock
        depth += 1

    _send_pipe_payload("l", all_vars)
    return "ok"


def _collect_stack():
    """Collect stack frames and send via data pipe (tag ``S``)."""
    frames = []
    try:
        frame = gdb.newest_frame()
    except gdb.error:
        _send_pipe_payload("s", [])
        return "ok"

    level = 0
    while frame:
        try:
            sal = frame.find_sal()
            func_name = frame.name() or ""
            addr = hex(frame.pc())
            if sal.symtab:
                file_name = sal.symtab.filename or ""
                fullname = sal.symtab.fullname() or ""
            else:
                file_name = ""
                fullname = ""
            line = sal.line
        except gdb.error:
            func_name = ""
            addr = "0x0"
            file_name = ""
            fullname = ""
            line = 0

        frames.append({
            "level": level,
            "func": func_name,
            "addr": addr,
            "file": file_name,
            "fullname": fullname,
            "line": line,
        })

        level += 1
        try:
            frame = frame.older()
        except gdb.error:
            break

    _send_pipe_payload("s", frames)
    return "ok"


def _collect_threads():
    """Collect thread info and send via data pipe (tag ``T``)."""
    try:
        inferior = gdb.selected_inferior()
        thread_list = list(inferior.threads())
    except gdb.error:
        _send_pipe_payload("t", {"threads": [], "current-thread-id": ""})
        return "ok"

    try:
        original_thread = gdb.selected_thread()
    except gdb.error:
        original_thread = None

    current_thread_id = ""
    if original_thread:
        current_thread_id = str(original_thread.global_num)

    threads = []
    for thread in thread_list:
        tid = str(thread.global_num)

        try:
            target_id = thread.ptid_string
        except AttributeError:
            pid, lwp, _tid = thread.ptid
            target_id = f"LWP {lwp}" if lwp else f"process {pid}"

        name = thread.name or ""
        is_stopped = thread.is_stopped()
        state = "stopped" if is_stopped else "running"

        frame_info = None
        if is_stopped:
            try:
                thread.switch()
                frame = gdb.selected_frame()
                sal = frame.find_sal()
                frame_info = {
                    "level": 0,
                    "func": frame.name() or "",
                    "addr": hex(frame.pc()),
                    "file": sal.symtab.filename if sal.symtab else "",
                    "fullname": sal.symtab.fullname() if sal.symtab else "",
                    "line": sal.line,
                }
            except gdb.error:
                pass

        threads.append({
            "id": tid,
            "target-id": target_id,
            "name": name,
            "state": state,
            "core": "",
            "frame": frame_info,
        })

    if original_thread:
        try:
            original_thread.switch()
        except gdb.error:
            pass

    _send_pipe_payload("t", {
        "threads": threads,
        "current-thread-id": current_thread_id,
    })
    return "ok"


def _collect_registers():
    """Collect register values and send via data pipe (tag ``R``)."""
    try:
        frame = gdb.selected_frame()
        arch = frame.architecture()
    except gdb.error:
        _send_pipe_payload("r", [])
        return "ok"

    registers = []
    try:
        for number, reg in enumerate(arch.registers()):
            if not reg.name:
                continue
            try:
                val = frame.read_register(reg)
                try:
                    val_str = val.format_string(format="x")
                except (TypeError, AttributeError):
                    val_str = str(val)
            except gdb.error:
                val_str = ""

            registers.append({
                "name": reg.name,
                "value": val_str,
                "number": number,
            })
    except (gdb.error, AttributeError):
        _send_pipe_payload("r", [])
        return "ok"

    _send_pipe_payload("r", registers)
    return "ok"


# ---------------------------------------------------------------------------
# Convenience function registrations for pipe-based collection
# ---------------------------------------------------------------------------


class _CollectLocalsFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_locals()`` — collect locals via data pipe."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_locals")


    def invoke(self):
        return _collect_locals()


class _CollectStackFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_stack()`` — collect stack frames via data pipe."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_stack")


    def invoke(self):
        return _collect_stack()


class _CollectThreadsFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_threads()`` — collect thread info via data pipe."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_threads")


    def invoke(self):
        return _collect_threads()


class _CollectRegistersFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_registers()`` — collect register values via data pipe."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_registers")


    def invoke(self):
        return _collect_registers()


_CollectLocalsFunc()
_CollectStackFunc()
_CollectThreadsFunc()
_CollectRegistersFunc()
