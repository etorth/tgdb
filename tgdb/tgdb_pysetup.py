import gdb
import json
import base64


def get_locals_b64():
    try:
        frame = gdb.selected_frame()
        block = frame.block()
    except gdb.error:
        return base64.b64encode(b"[]").decode("ascii")

    # Only show variables declared on or before the current executing line.
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

            # Skip variables whose declaration comes after the current line.
            # Arguments always pass (their "line" is the function signature
            # line, which may equal current_line on entry — always show them).
            decl_line = symbol.line
            if current_line > 0 and not symbol.is_argument:
                if decl_line > 0 and decl_line > current_line:
                    continue

            name = symbol.name

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
                    except gdb.error:
                        addr_str = "unknown (referenced target)"
                else:
                    addr_str = str(val.address) if val.address else "register"

            except gdb.error:
                val_str = "<optimized out>"
                addr_str = "unknown"

            is_shadowed = name in seen_names

            all_vars.append({
                "name": name,
                "value": val_str,
                "type": str(symbol.type),
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
