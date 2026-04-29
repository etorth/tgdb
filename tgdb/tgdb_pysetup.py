import gdb
import json
import base64
import os

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


def get_locals_b64():
    try:
        frame = gdb.selected_frame()
        block = frame.block()
    except gdb.error:
        return base64.b64encode(b"[]").decode("ascii")

    # Only show variables declared before the current executing line.
    # sal.line is 0 when no line info is available; in that case we fall back
    # to showing everything (line filter disabled).
    sal = frame.find_sal()
    current_line = sal.line  # 0 means unknown

    all_vars = []
    seen_names = set()

    while block:
        for symbol in block:
            if not (symbol.is_variable or symbol.is_argument):
                continue

            name = symbol.name
            if _is_builtin_local_name(name):
                continue

            # Skip variables whose declaration comes at or after the current line.
            # Arguments always pass (their "line" is the function signature
            # line, which may equal current_line on entry — always show them).
            decl_line = symbol.line
            if current_line > 0 and not symbol.is_argument:
                if decl_line > 0 and decl_line >= current_line:
                    continue

            # Get the underlying type code to detect references.
            # strip_typedefs() handles 'typedef int& my_ref_type;' etc.
            type_obj = symbol.type.strip_typedefs()

            is_lref = type_obj.code == gdb.TYPE_CODE_REF
            is_rref = type_obj.code == gdb.TYPE_CODE_RVALUE_REF
            is_reference = is_lref or is_rref

            try:
                val = symbol.value(frame)
                val_str = str(val)

                # If it's a reference, val.address is the address of the
                # metadata/pointer; val.referenced_value().address is the
                # actual object.
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

            all_vars.append({
                "name": name,
                "value": val_str,
                "type": str(symbol.type),
                "is_arg": symbol.is_argument,
                "is_reference": is_reference,
                "ref_kind": "lvalue (&)" if is_lref else ("rvalue (&&)" if is_rref else None),
                "line": decl_line,
                "addr": addr_str,
                "is_shadowed": is_shadowed,
                "scope_start": hex(block.start),
            })

            seen_names.add(name)

        # Stop after processing the global block or static block.
        if block.superblock is None:
            break

        # Stop after processing the function's top-level block;
        # variables from the caller are not locals.
        if block.function is not None:
            break

        block = block.superblock

    return base64.b64encode(json.dumps(all_vars, indent=2).encode()).decode("ascii")


class _GetLocalsB64Func(gdb.Function):
    """GDB convenience function ``$get_locals_b64()`` backed by get_locals_b64().

    Instantiating this class registers ``$get_locals_b64`` with GDB's Python
    runtime as a side effect of gdb.Function.__init__().
    """

    def __init__(self):
        super().__init__("get_locals_b64")

    def invoke(self):
        return get_locals_b64()


_GetLocalsB64Func()
