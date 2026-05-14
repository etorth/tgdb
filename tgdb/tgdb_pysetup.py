import gdb
import json
import logging
import os
import struct
import threading
import zlib

# Lift GDB's memory read limit so str(val) never fails for large variables.
try:
    gdb.execute("set max-value-size unlimited", to_string=True)
except gdb.error:
    pass


_sock_fd = None
_event_handlers_connected = False

# ---------------------------------------------------------------------------
# Cancel-token infrastructure
#
# tgdb writes 4-byte big-endian unsigned integers (cancel tokens) to the
# socket.  A daemon reader thread on the GDB side drains them into a set.
# Convenience functions check the set at key points and abort early when
# their token is present — returning ``"cancelled"`` instead of ``"ok"``.
#
# Token 0 means "no cancellation support" and is never checked.
# ---------------------------------------------------------------------------

_cancel_tokens: set[int] = set()
_cancel_lock = threading.Lock()
_cancel_reader_started = False

# Control-byte bit masks for variable-length socket frames.
_CTL_COMPRESSED = 0x01

# Payloads at or above this size are zlib-compressed automatically.
_COMPRESS_THRESHOLD = 64


# ---------------------------------------------------------------------------
# GDB-side logger
#
# Mirrors the tgdb-side pattern in ``tgdb/log.py``:
# - Default level is WARNING so ``_log.debug(...)`` calls are free when
#   tgdb is run without ``--log``.
# - When tgdb passes ``log_enabled=True`` via ``register_socket_fd()``,
#   the level is raised to DEBUG and a custom handler sends messages
#   through the socket (tag ``D``) to appear in tgdb's log file.
# ---------------------------------------------------------------------------

class _SocketLogHandler(logging.Handler):
    """Logging handler that sends records through the GDB↔tgdb socket."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _send_sock_frame("D", msg.encode("utf-8", errors="replace"))
        except Exception:
            pass


_log = logging.getLogger("tgdb.gdb_python")
_log.addHandler(logging.NullHandler())
_log.setLevel(logging.WARNING)
_log.propagate = False


def _send_sock_frame(tag, payload):
    """Write a variable-length frame to the socket.

    Frame format: ``[tag 1B][ctl 1B][length 7B BE][payload]``.

    *tag* is a single ASCII byte (string or bytes).  *payload* is raw
    bytes.  If the payload length meets ``_COMPRESS_THRESHOLD``, it is
    zlib-compressed and the ``_CTL_COMPRESSED`` bit is set in the control
    byte; otherwise the payload is sent as-is.

    Returns True on success, False if the socket is closed or the write fails.
    """
    fd = _sock_fd
    if fd is None:
        return False

    tag_byte = tag.encode("ascii")[:1] if isinstance(tag, str) else tag[:1]

    if len(payload) >= _COMPRESS_THRESHOLD:
        payload = zlib.compress(payload)
        ctl = _CTL_COMPRESSED
    else:
        ctl = 0x00

    length_bytes = struct.pack(">Q", len(payload))[1:]
    buf = tag_byte + bytes([ctl]) + length_bytes + payload
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


def _start_cancel_reader(fd):
    """Start a daemon thread that reads cancel tokens from the socket.

    tgdb writes 4-byte big-endian unsigned integers to the socket.  This
    thread drains them into ``_cancel_tokens`` so convenience functions can
    check for cancellation without blocking the GDB main thread.

    The thread exits silently when the socket closes (empty read or OSError).
    """
    global _cancel_reader_started
    if _cancel_reader_started:
        return
    _cancel_reader_started = True

    def _reader():
        buf = b""
        while True:
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                buf += data
                while len(buf) >= 4:
                    token = struct.unpack(">I", buf[:4])[0]
                    buf = buf[4:]
                    with _cancel_lock:
                        _cancel_tokens.add(token)
                    _log.debug(f"cancel token received: {token}")
            except OSError:
                break

    t = threading.Thread(target=_reader, daemon=True, name="tgdb-cancel-reader")
    t.start()


def _is_cancelled(token):
    """Return True if *token* has been cancelled by tgdb."""
    if token == 0:
        return False
    with _cancel_lock:
        return token in _cancel_tokens


def _finish_token(token):
    """Remove *token* from the cancel set (completed or cancelled)."""
    if token == 0:
        return
    with _cancel_lock:
        _cancel_tokens.discard(token)


def register_socket_fd(fd, log_enabled=False):
    """Wire GDB Python events and data collection to an AF_UNIX socket.

    tgdb creates an ``AF_UNIX`` socketpair before forking GDB and passes
    one end's fd number here.  All communication uses a tag-driven binary
    frame format so lightweight events and bulk data share the same
    channel.  The socket is bidirectional: GDB writes data/events to
    tgdb, and tgdb can write cancel tokens back to GDB.

    When *log_enabled* is True (tgdb was started with ``--log``), the
    GDB-side logger is raised to DEBUG and a ``_SocketLogHandler`` is
    attached so ``_log.debug(...)`` messages flow through the socket to
    tgdb's log file.  When False, the logger stays at WARNING and all
    DEBUG-level calls are effectively free (no string formatting, no
    handler walk).

    See ``docs/socket-protocol.md`` for the full protocol specification.

    Calling this again with a different fd retargets the existing handlers.
    Handlers are connected to GDB's event registries exactly once per
    Python process so a re-call cannot accumulate duplicates.
    """
    global _sock_fd, _event_handlers_connected
    _sock_fd = fd

    # Start the cancel-token reader thread.  It reads 4-byte BE unsigned
    # integers from the socket (written by tgdb) and adds them to
    # ``_cancel_tokens``.  Started once per process.
    _start_cancel_reader(fd)

    if log_enabled:
        # Remove any previously attached socket handlers (in case of re-init).
        for handler in list(_log.handlers):
            if isinstance(handler, _SocketLogHandler):
                _log.removeHandler(handler)
        handler = _SocketLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        _log.addHandler(handler)
        _log.setLevel(logging.DEBUG)
    else:
        _log.setLevel(logging.WARNING)

    if _event_handlers_connected:
        return
    _event_handlers_connected = True

    def _emit(tag_byte, payload=b""):
        active_fd = _sock_fd
        if active_fd is None:
            return
        try:
            if payload:
                os.write(active_fd, tag_byte + payload)
            else:
                os.write(active_fd, tag_byte)
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


def _send_sock_payload(tag, data):
    """Serialize *data* as JSON and write a framed payload to the socket.

    Uses the unified variable-length frame format.  *tag* must be a single
    ASCII character (one of ``l``, ``s``, ``r``, ``f``, ``b``).
    Compression is applied automatically when the JSON exceeds the threshold.
    Returns True on success, False if the socket is closed or the write fails.
    """
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return _send_sock_frame(tag, json_bytes)


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
    """Return True for compiler-generated or implicit locals we should hide.

    Exact-match names (``_BUILTIN_LOCAL_NAMES``):

    - ``_`` — intentionally-ignored scratch binding
    - ``__func__``, ``__FUNCTION__``, ``__PRETTY_FUNCTION__`` — GCC/Clang
      implicit ``static const char[]`` injected into every function body
    - ``__FUNCSIG__`` — MSVC equivalent of ``__PRETTY_FUNCTION__``
    - ``__in_chrg`` — GCC complete-vs-base constructor/destructor flag
    - ``__vtt_parm`` — GCC virtual table table pointer for virtual bases

    Prefix-match families (``_BUILTIN_LOCAL_PREFIXES``):

    - ``__for_`` — C++ range-for lowering (``__for_begin``, ``__for_end``,
      ``__for_range``)
    - ``__range`` — alternate range-for lowering in some compilers
    - ``__guard`` — static local initialization guard variables

    All of these are noise in the locals pane and never useful to inspect.
    """
    if not name:
        return False

    if name in _BUILTIN_LOCAL_NAMES:
        return True

    for prefix in _BUILTIN_LOCAL_PREFIXES:
        if name.startswith(prefix):
            return True

    return False


_BUILTIN_LOCAL_NAMES = frozenset({
    "_",
    "__func__",
    "__FUNCTION__",
    "__PRETTY_FUNCTION__",
    "__FUNCSIG__",
    "__in_chrg",
    "__vtt_parm",
})

_BUILTIN_LOCAL_PREFIXES = (
    "__for_",
    "__range",
    "__guard",
)


# ---------------------------------------------------------------------------
# Socket-based collection functions
#
# Each function collects data using GDB's Python API, serializes it as
# JSON, zlib-compresses the bytes, and writes a length-prefixed frame to
# the socket.  The MI return value is a tiny "ok" string so the MI
# channel is never congested by the payload.
# ---------------------------------------------------------------------------


def _collect_locals(cancel_token=0):
    """Collect local variables and send via data socket (tag ``l``).

    If *cancel_token* is non-zero and has been cancelled by tgdb, the
    function aborts early and returns ``"cancelled"`` without sending
    any data through the socket.
    """
    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    try:
        frame = gdb.selected_frame()
    except gdb.error:
        _send_sock_payload("l", [])
        _finish_token(cancel_token)
        return "ok"

    try:
        block = frame.block()
    except (gdb.error, RuntimeError) as exc:
        if "Cannot locate block" in str(exc):
            _send_sock_payload("l", [])
            _finish_token(cancel_token)
            return "ok"
        raise

    sal = frame.find_sal()
    current_line = sal.line

    depth = 0
    all_vars = []

    # Map name → {line, type, has_real_addr} from the shallowest depth seen.
    # Used to detect GDB Bug 3 noise: a deeper-depth copy of the same
    # variable (same name, same decl line, same type) whose address is
    # synthetic register@N is a phantom merged from the parent block.
    seen: dict[str, dict] = {}

    while block:
        if _is_cancelled(cancel_token):
            _finish_token(cancel_token)
            return "cancelled"

        block_start = hex(block.start)
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
                    except Exception as exc:
                        addr_str = "unknown (referenced target)"
                        _log.debug(f"referenced_value().address failed for {name}: {exc}")
                else:
                    if val.address:
                        addr_str = str(val.address)
                    else:
                        addr_str = f"register@{depth}"

            except Exception as exc:
                val_str = "<optimized out>"
                addr_str = "unknown"
                _log.debug(f"value eval failed for {name}: {exc}")

            is_shadowed = name in seen
            type_str = str(symbol.type)

            # GDB Bug 3 workaround: a variable at a deeper depth that has
            # the same name, declaration line, and type as one already seen
            # at a shallower depth — but whose address is a synthetic
            # "register@N" (val.address was None) — is a phantom copy
            # merged by GDB from the parent function-body block.  The
            # shallower copy has the real stack address and value; this
            # deeper copy is noise.  See docs/known_gdb_bug.md § Bug 3.
            if is_shadowed and addr_str.startswith("register@"):
                prev = seen[name]
                if prev["line"] == decl_line and prev["type"] == type_str and prev["has_real_addr"]:
                    continue

            if is_lref:
                ref_kind = "lvalue (&)"
            elif is_rref:
                ref_kind = "rvalue (&&)"
            else:
                ref_kind = None

            all_vars.append({
                "name": name,
                "value": val_str,
                "type": type_str,
                "is_arg": symbol.is_argument,
                "is_reference": is_reference,
                "ref_kind": ref_kind,
                "line": decl_line,
                "addr": addr_str,
                "depth": depth,
                "is_shadowed": is_shadowed,
                "scope_start": hex(block.start),
            })

            if name not in seen:
                seen[name] = {
                    "line": decl_line,
                    "type": type_str,
                    "has_real_addr": not addr_str.startswith("register@") and addr_str != "unknown",
                }

        if block.superblock is None:
            break

        if block.function is not None:
            break

        block = block.superblock
        depth += 1

    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    # GDB Bug 3 workaround: deduplicate register variables with identical
    # (name, addr) keys.  GDB's block iterator merges sibling-scope symbols
    # into the parent function-body block and can yield the same variable
    # twice within a single block.  Register variables all share the
    # synthetic addr "register@<depth>", producing duplicate BindingKeys.
    # Keep only the first entry per (name, addr) that carries a real value;
    # fall back to the first entry if all are optimized out.
    # See docs/known_gdb_bug.md § Bug 3.
    deduped: list[dict] = []
    seen_keys: dict[tuple[str, str], int] = {}
    for entry in all_vars:
        key = (entry["name"], entry["addr"])
        if key not in seen_keys:
            seen_keys[key] = len(deduped)
            deduped.append(entry)
        elif entry["value"] != "<optimized out>":
            prev_idx = seen_keys[key]
            if deduped[prev_idx]["value"] == "<optimized out>":
                deduped[prev_idx] = entry

    _send_sock_payload("l", deduped)
    _finish_token(cancel_token)
    return "ok"


def _collect_stack(cancel_token=0):
    """Collect stack frames and send via data socket (tag ``s``)."""
    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    frames = []
    try:
        frame = gdb.newest_frame()
    except gdb.error:
        _send_sock_payload("s", [])
        _finish_token(cancel_token)
        return "ok"

    level = 0
    while frame:
        if level % 50 == 0 and _is_cancelled(cancel_token):
            _finish_token(cancel_token)
            return "cancelled"

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

    _send_sock_payload("s", frames)
    _finish_token(cancel_token)
    return "ok"


def _collect_registers(cancel_token=0):
    """Collect register values and send via data socket (tag ``r``)."""
    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    try:
        frame = gdb.selected_frame()
        arch = frame.architecture()
    except gdb.error:
        _send_sock_payload("r", [])
        _finish_token(cancel_token)
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
        _send_sock_payload("r", [])
        _finish_token(cancel_token)
        return "ok"

    _send_sock_payload("r", registers)
    _finish_token(cancel_token)
    return "ok"


def _collect_frame_info(cancel_token=0):
    """Collect current frame info and send via data socket (tag ``f``).

    Mirrors the data that ``-stack-info-frame`` returns: level, func,
    addr, file, fullname, line, arch.
    """
    try:
        frame = gdb.selected_frame()
    except gdb.error:
        _send_sock_payload("f", {})
        _finish_token(cancel_token)
        return "ok"

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
        _send_sock_payload("f", {})
        _finish_token(cancel_token)
        return "ok"

    try:
        arch_name = frame.architecture().name()
    except (gdb.error, AttributeError):
        arch_name = ""

    _send_sock_payload("f", {
        "level": frame.level(),
        "func": func_name,
        "addr": addr,
        "file": file_name,
        "fullname": fullname,
        "line": line,
        "arch": arch_name,
    })
    _finish_token(cancel_token)
    return "ok"


def _collect_breakpoints(cancel_token=0):
    """Collect breakpoint info and send via data socket (tag ``b``).

    Mirrors the data that ``-break-list`` returns.
    """
    breakpoints = []
    for bp in gdb.breakpoints():
        if not bp.is_valid():
            continue
        loc = bp.location or ""
        fullname = ""
        file_name = ""
        line_num = 0

        if bp.location:
            try:
                sal = gdb.decode_line(bp.location)
                if sal and sal[1]:
                    first_sal = sal[1][0]
                    if first_sal.symtab:
                        file_name = first_sal.symtab.filename or ""
                        fullname = first_sal.symtab.fullname() or ""
                    line_num = first_sal.line
            except (gdb.error, IndexError, TypeError):
                pass

        breakpoints.append({
            "number": bp.number,
            "file": file_name,
            "fullname": fullname,
            "line": line_num,
            "addr": "",
            "enabled": bp.enabled,
            "temporary": bp.temporary,
            "location": loc,
        })

    _send_sock_payload("b", breakpoints)
    _finish_token(cancel_token)
    return "ok"


# ---------------------------------------------------------------------------
# Convenience function registrations for socket-based collection
# ---------------------------------------------------------------------------


class _CollectLocalsFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_locals([token])`` — collect locals via data socket."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_locals")


    def invoke(self, *args):
        token = int(args[0]) if args else 0
        return _collect_locals(token)


class _CollectStackFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_stack([token])`` — collect stack frames via data socket."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_stack")


    def invoke(self, *args):
        token = int(args[0]) if args else 0
        return _collect_stack(token)


class _CollectRegistersFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_registers([token])`` — collect register values via data socket."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_registers")


    def invoke(self, *args):
        token = int(args[0]) if args else 0
        return _collect_registers(token)


class _CollectFrameInfoFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_frame_info([token])`` — collect current frame via data socket."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_frame_info")


    def invoke(self, *args):
        token = int(args[0]) if args else 0
        return _collect_frame_info(token)


class _CollectBreakpointsFunc(gdb.Function):
    """``$_tgdb_RSVD_collect_breakpoints([token])`` — collect breakpoints via data socket."""

    def __init__(self):
        super().__init__("_tgdb_RSVD_collect_breakpoints")


    def invoke(self, *args):
        token = int(args[0]) if args else 0
        return _collect_breakpoints(token)


_CollectLocalsFunc()
_CollectStackFunc()
_CollectRegistersFunc()
_CollectFrameInfoFunc()
_CollectBreakpointsFunc()
