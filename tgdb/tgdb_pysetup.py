import gdb
import json
import os
import zlib

# Lift GDB's memory read limit so str(val) never fails for large variables.
try:
    gdb.execute("set max-value-size unlimited", to_string=True)
except gdb.error:
    pass


_event_notify_fd = None
_event_handlers_connected = False


def register_event_notify_fd(fd):
    """Wire GDB Python events to the inheritable notify pipe.

    tgdb opens an inheritable pipe before forking GDB and passes the write
    end's fd number here. Each event is encoded as a single newline-
    terminated line so tgdb can parse them out of the byte stream. The
    line format is ``<TAG><payload>\\n`` where TAG is a single uppercase
    ASCII char:

      ``P``         before_prompt — refresh selected frame in source pane.
      ``R<regnum>`` register_changed — user wrote a register
                    (e.g. ``set $rax=…``).  GDB has no MI async record
                    for this; without the hook the register pane would
                    only update on the next ``*stopped``.
      ``O``         new_objfile — a shared library was just loaded.
                    Coalesced; the dispatcher fires once per burst.
      ``F``         free_objfile — a shared library was unloaded.
      ``C``         clear_objfiles — program space was wiped (e.g. ``kill``).
      ``Ipre``      inferior_call_pre — user expression about to call into
                    the inferior (``print foo()``); freeze polling state.
      ``Ipost``     inferior_call_post — call returned; refresh locals,
                    registers, and memory because the inferior just ran.
      ``X``         gdb_exiting — GDB's main loop is tearing down.  Lets
                    tgdb shut down promptly instead of waiting for PTY EOF.

    Calling this again with a different fd just retargets the existing
    handlers (the new fd is what the next event writes to).  Handlers are
    connected to GDB's event registries exactly once per Python process
    so a re-call cannot accumulate duplicates.

    Handlers are deliberately tiny — they run on GDB's main thread and a
    slow handler would stall the next prompt.  Errors writing the pipe
    are swallowed so a dead/closed reader can't kill GDB.
    """
    global _event_notify_fd, _event_handlers_connected
    _event_notify_fd = fd
    if _event_handlers_connected:
        return
    _event_handlers_connected = True

    def _emit(line_bytes):
        active_fd = _event_notify_fd
        if active_fd is None:
            return
        try:
            os.write(active_fd, line_bytes)
        except (BlockingIOError, OSError):
            pass

    def _on_before_prompt():
        _emit(b"P\n")

    def _on_register_changed(event):
        try:
            regnum = int(event.regnum)
        except (AttributeError, ValueError, TypeError):
            regnum = -1
        _emit(f"R{regnum}\n".encode())

    def _on_new_objfile(_event):
        _emit(b"O\n")

    def _on_free_objfile(_event):
        _emit(b"F\n")

    def _on_clear_objfiles(_event):
        _emit(b"C\n")

    def _on_inferior_call(event):
        if isinstance(event, gdb.InferiorCallPreEvent):
            _emit(b"Ipre\n")
        else:
            _emit(b"Ipost\n")

    def _on_gdb_exiting(_event):
        _emit(b"X\n")

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


_data_pipe_fd = None


def register_data_pipe_fd(fd):
    """Register the data pipe fd for bulk data transfer to tgdb.

    tgdb opens a second inheritable pipe dedicated to large payloads.
    The GDB-side collection functions serialize data as JSON, compress
    it with zlib, and write length-prefixed frames to this fd.  The
    event-notify pipe stays separate for lightweight event lines.

    Frame format: ``[4-byte BE length][1-byte tag][zlib-compressed JSON]``.
    """
    global _data_pipe_fd
    _data_pipe_fd = fd


def _send_pipe_payload(tag, data):
    """Serialize *data* as JSON, zlib-compress, and write a framed payload.

    Returns True on success, False if the pipe is not registered or the
    write fails.
    """
    fd = _data_pipe_fd
    if fd is None:
        return False
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(json_bytes)
    tag_byte = tag.encode("ascii")[:1] if isinstance(tag, str) else tag[:1]
    payload = tag_byte + compressed
    header = len(payload).to_bytes(4, "big")
    buf = header + payload
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
        _send_pipe_payload("L", [])
        return "ok"

    try:
        block = frame.block()
    except (gdb.error, RuntimeError) as exc:
        if "Cannot locate block" in str(exc):
            _send_pipe_payload("L", [])
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

    _send_pipe_payload("L", all_vars)
    return "ok"


def _collect_stack():
    """Collect stack frames and send via data pipe (tag ``S``)."""
    frames = []
    try:
        frame = gdb.newest_frame()
    except gdb.error:
        _send_pipe_payload("S", [])
        return "ok"

    level = 0
    while frame:
        try:
            sal = frame.find_sal()
            func_name = frame.name() or ""
            addr = hex(frame.pc())
            if sal.symtab:
                file_name = sal.symtab.filename or ""
                fullname = sal.symtab.fullname or ""
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

    _send_pipe_payload("S", frames)
    return "ok"


def _collect_threads():
    """Collect thread info and send via data pipe (tag ``T``)."""
    try:
        inferior = gdb.selected_inferior()
        thread_list = list(inferior.threads())
    except gdb.error:
        _send_pipe_payload("T", {"threads": [], "current-thread-id": ""})
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
                    "fullname": sal.symtab.fullname if sal.symtab else "",
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

    _send_pipe_payload("T", {
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
        _send_pipe_payload("R", [])
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
        _send_pipe_payload("R", [])
        return "ok"

    _send_pipe_payload("R", registers)
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
