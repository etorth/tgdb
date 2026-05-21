import gdb
import json
import logging
import os
import threading
import zlib

_sock_fd = None
_event_handlers_connected = False

# ---------------------------------------------------------------------------
# Cancel-token infrastructure
#
# tgdb writes varint-encoded unsigned integers (cancel tokens) to the
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

def _str_unlimited(val) -> str:
    with gdb.with_parameter("max-value-size", "unlimited"):
        return str(val)


# ---------------------------------------------------------------------------
# Pagination suppression for attach
#
# When tgdb spawns GDB with -p <pid>, GDB may print many "[New LWP ...]"
# lines that overflow the terminal height and trigger pagination prompts.
# pysetup is sourced before -p on the command line, so we set height to a
# magic value large enough to suppress pagination.  After attach completes,
# _tgdb_RSVD_restore_user_defs() restores the original height — but only
# if no user -ex command has overridden it in the meantime.
# ---------------------------------------------------------------------------

# GDB's ``set height`` maximum is 32767.  Use the closest prime as a
# sentinel: if the value is still this when restore runs, no user -ex
# command changed it and we should restore the saved value.
_TGDB_INIT_HEIGHT = 32749

_saved_height = gdb.parameter("height") or 0
gdb.execute(f"set height {_TGDB_INIT_HEIGHT}", to_string=True)


def _tgdb_RSVD_restore_user_defs():
    """Restore user settings that were overridden for safe attach."""
    if gdb.parameter("height") == _TGDB_INIT_HEIGHT:
        gdb.execute(f"set height {_saved_height}", to_string=True)

# ---------------------------------------------------------------------------
# Varint helpers — unsigned LEB128
#
# Each byte carries 7 data bits; MSB=1 means "more bytes follow".
# ---------------------------------------------------------------------------

def _encode_varint(n):
    """Encode unsigned integer *n* as LEB128 varint bytes."""
    buf = bytearray()
    while n >= 0x80:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)
    return bytes(buf)


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

    Frame format: ``[tag 1B][ctl 1B][length varint][payload]``.

    *tag* is a single ASCII byte (string or bytes).  *payload* is raw
    bytes.  If the payload length meets ``_COMPRESS_THRESHOLD``, it is
    zlib-compressed and the ``_CTL_COMPRESSED`` bit is set in the control
    byte; otherwise the payload is sent as-is.

    Returns True on success, False if the socket is closed or the write fails.
    """
    fd = _sock_fd
    if fd is None:
        return False

    if isinstance(tag, str):
        tag_byte = tag.encode("ascii")[:1]
    else:
        tag_byte = tag[:1]

    if len(payload) >= _COMPRESS_THRESHOLD:
        payload = zlib.compress(payload)
        ctl = _CTL_COMPRESSED
    else:
        ctl = 0x00

    length_bytes = _encode_varint(len(payload))
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

    tgdb writes varint-encoded unsigned integers to the socket.  This
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
                while buf:
                    token, after = _decode_varint(buf)
                    if token is None:
                        break
                    buf = buf[after:]
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

    # Start the cancel-token reader thread.  It reads varint-encoded
    # unsigned integers from the socket (written by tgdb) and adds them
    # to ``_cancel_tokens``.  Started once per process.
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

    def _on_register_changed(event):
        try:
            regnum = int(event.regnum)
        except (AttributeError, ValueError, TypeError):
            regnum = -1
        _emit(b"R", _encode_varint(regnum + 1))

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


def _send_sock_payload(tag, data, token=0):
    """Serialize *data* as JSON and write a framed payload to the socket.

    Uses the unified variable-length frame format.  *tag* must be a single
    ASCII character (one of ``l``, ``s``, ``r``, ``f``, ``b``).
    Compression is applied automatically when the JSON exceeds the threshold.

    When *token* is non-zero, a varint-encoded MI token is prepended to
    the payload so the tgdb side can correlate this data with the MI
    command that triggered the collection.

    Returns True on success, False if the socket is closed or the write fails.
    """
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    payload = _encode_varint(token) + json_bytes
    return _send_sock_frame(tag, payload)


def _format_value(val):
    """Format a gdb.Value with unlimited elements per-call.

    Uses ``format_string(max_elements=0)`` when available (GDB 9.1+)
    to avoid contaminating global ``set print elements`` settings.
    Falls back to ``_str_unlimited(val)`` on older builds.
    """
    try:
        return val.format_string(max_elements=0)
    except (TypeError, AttributeError):
        return _str_unlimited(val)


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
    function aborts early and returns ``"cancelled"``.  The tgdb side
    detects ``"cancelled"`` in the MI value and resolves the Future
    immediately — no socket payload is needed.
    """
    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    try:
        frame = gdb.selected_frame()
    except gdb.error:
        _send_sock_payload("l", [], cancel_token)
        _finish_token(cancel_token)
        return "done"

    try:
        block = frame.block()
    except (gdb.error, RuntimeError) as exc:
        if "Cannot locate block" in str(exc):
            _send_sock_payload("l", [], cancel_token)
            _finish_token(cancel_token)
            return "done"
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

    _send_sock_payload("l", deduped, cancel_token)
    _finish_token(cancel_token)
    return "done"


def _collect_stack(cancel_token=0):
    """Collect stack frames and send via data socket (tag ``s``)."""
    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    frames = []
    try:
        frame = gdb.newest_frame()
    except gdb.error:
        _send_sock_payload("s", [], cancel_token)
        _finish_token(cancel_token)
        return "done"

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

    _send_sock_payload("s", frames, cancel_token)
    _finish_token(cancel_token)
    return "done"


def _collect_registers(cancel_token=0):
    """Collect register values and send via data socket (tag ``r``)."""
    if _is_cancelled(cancel_token):
        _finish_token(cancel_token)
        return "cancelled"

    try:
        frame = gdb.selected_frame()
        arch = frame.architecture()
    except gdb.error:
        _send_sock_payload("r", [], cancel_token)
        _finish_token(cancel_token)
        return "done"

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
                    val_str = _str_unlimited(val)
            except gdb.error:
                val_str = ""

            registers.append({
                "name": reg.name,
                "value": val_str,
                "number": number,
            })
    except (gdb.error, AttributeError):
        _send_sock_payload("r", [], cancel_token)
        _finish_token(cancel_token)
        return "done"

    _send_sock_payload("r", registers, cancel_token)
    _finish_token(cancel_token)
    return "done"


def _collect_frame_info(cancel_token=0):
    """Collect current frame info and send via data socket (tag ``f``).

    Mirrors the data that ``-stack-info-frame`` returns: level, func,
    addr, file, fullname, line, arch.
    """
    try:
        frame = gdb.selected_frame()
    except gdb.error:
        _send_sock_payload("f", {}, cancel_token)
        _finish_token(cancel_token)
        return "done"

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
        _send_sock_payload("f", {}, cancel_token)
        _finish_token(cancel_token)
        return "done"

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
    }, cancel_token)
    _finish_token(cancel_token)
    return "done"


def _collect_breakpoints(cancel_token=0):
    """Collect breakpoint info and send via data socket (tag ``b``).

    Emits one entry per addressable location.  For multi-location
    breakpoints (template instantiations, inlined call sites, ...) the
    parent is omitted and each ``gdb.BreakpointLocation`` is emitted as
    a separate entry with a dotted id like ``"3.1"`` — matching the
    flatten model the tgdb-side parser expects.  Mirrors the structure
    of ``-break-list`` output, including its ``locations=[...]`` shape.
    """
    breakpoints = []
    for bp in gdb.breakpoints():
        if not bp.is_valid():
            continue

        loc_str = bp.location or ""

        # gdb.BreakpointLocation was added in GDB 13; older GDBs lack
        # the ``locations`` attribute entirely.  Feature-detect once
        # per breakpoint instead of failing the whole collection.
        bp_locations = None
        try:
            bp_locations = bp.locations
        except AttributeError:
            bp_locations = None

        if bp_locations:
            # Multi-location case: emit one entry per child.  Child id
            # is ``"<parent>.<1-based index>"``; this is the same form
            # GDB prints in ``info breakpoints`` and in MI's
            # ``locations`` array.
            for idx, child in enumerate(bp_locations, start=1):
                try:
                    src = child.source  # (filename, line) or None
                except (gdb.error, RuntimeError):
                    src = None
                file_name = ""
                fullname = ""
                line_num = 0
                if src is not None:
                    try:
                        symtab, line_num = src
                        if symtab is not None:
                            file_name = symtab.filename or ""
                            fullname = symtab.fullname() or ""
                    except (TypeError, ValueError, AttributeError):
                        pass
                # ``child.fullname`` (string) exists on newer GDBs and
                # may be richer than the symtab path; prefer it when
                # available.
                try:
                    cf = child.fullname
                    if cf:
                        fullname = cf
                except (AttributeError, gdb.error, RuntimeError):
                    pass

                try:
                    addr = "0x%x" % int(child.address)
                except (AttributeError, TypeError, ValueError, gdb.error, RuntimeError):
                    addr = ""

                try:
                    child_enabled = bool(child.enabled)
                except (AttributeError, gdb.error, RuntimeError):
                    child_enabled = bool(bp.enabled)

                breakpoints.append({
                    "number": "%d.%d" % (bp.number, idx),
                    "file": file_name,
                    "fullname": fullname,
                    "line": int(line_num or 0),
                    "addr": addr,
                    "enabled": child_enabled,
                    "temporary": bp.temporary,
                    "location": loc_str,
                })
            continue

        # Single-location fallback: keep the prior decode_line-based
        # path so older GDBs (no BreakpointLocation) still produce
        # something useful.
        file_name = ""
        fullname = ""
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
            "number": str(bp.number),
            "file": file_name,
            "fullname": fullname,
            "line": int(line_num or 0),
            "addr": "",
            "enabled": bp.enabled,
            "temporary": bp.temporary,
            "location": loc_str,
        })

    _send_sock_payload("b", breakpoints, cancel_token)
    _finish_token(cancel_token)
    return "done"


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
