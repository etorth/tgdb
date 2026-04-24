import gdb
import json
import base64

def get_all_locals():
    try:
        frame = gdb.selected_frame()
        block = frame.block()
    except gdb.error:
        return []

    all_vars = []
    seen_names = set()

    while block:
        for symbol in block:
            if symbol.is_variable or symbol.is_argument:
                name = symbol.name

                # 1. Get the underlying type code to detect references
                # strip_typedefs() handles cases like 'typedef int& my_ref_type;'
                type_obj = symbol.type.strip_typedefs()

                is_lref = type_obj.code == gdb.TYPE_CODE_REF
                is_rref = type_obj.code == gdb.TYPE_CODE_RVALUE_REF
                is_reference = is_lref or is_rref

                try:
                    val = symbol.value(frame)
                    val_str = str(val)

                    # 2. Address Logic:
                    # If it's a reference, val.address is often the address of the
                    # metadata/pointer. val.referenced_value().address is the actual object.
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
                    "line": symbol.line,
                    "addr": addr_str,
                    "is_shadowed": is_shadowed,
                    "scope_start": hex(block.start)
                })

                seen_names.add(name)

        # Stop after processing the global block or static block
        if block.superblock is None:
            break

        # Optional: Stop after reaching the function boundary
        # but ONLY after processing the function block itself.
        if block.function is not None:
            # We just processed the function's top-level block;
            # usually, we stop here for "locals".
            break

        block = block.superblock

    return base64.b64encode(json.dump(all_vars, indent=2))
