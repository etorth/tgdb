# Anonymous Namespace Variable Handling

## Problem

GDB prints types from anonymous namespaces as `(anonymous namespace)::Foo`.
When the locals pane tries to create a varobj using the standard address-cast
expression `*((anonymous namespace)::Foo*)0x7fff...`, GDB's expression parser
rejects it — the parentheses in `(anonymous namespace)` are interpreted as the
start of a C-style cast, causing a syntax error.

This means any variable whose type lives in an anonymous namespace cannot be
inspected via the normal address-based varobj creation path.

## Solution: Name-Based Fallback

When the type string contains `(anonymous namespace)`, fall back to creating
the varobj by the variable's plain name:

```
-var-create - * "a"
```

GDB resolves the name to the **innermost scope** at the current PC. This
works correctly for a single variable. Complications arise when multiple
variables share the same name (shadowing).

## Depth Field

`get_locals_b64()` returns a `depth` field for each variable:

- `depth = 0` → innermost block (the block containing the current PC)
- `depth = 1` → one enclosing scope up
- `depth = N` → N enclosing scopes up

This field is critical for determining which variable GDB's name resolution
will bind to.

## Ordering

Variables are **not sorted** before insertion into the locals pane. The
original order from `get_locals_b64()` is preserved (declaration order,
newest last). When variables are declared on the same line, the original
order is also preserved.

The depth field is used only for the anonymous namespace decision logic,
not for display ordering.

## Shadowing Rules

When multiple same-named variables with unparseable types coexist,
`_add_new_bindings` precomputes `min_depth_by_name` — the smallest depth
among same-named unparseable-type variables across all current bindings.

Each variable entering `_add_binding_by_name_fallback` is checked:

1. **`variable.depth == min_depth_by_name[name]`** → this variable has the
   smallest depth, it wins the name. Create a real varobj via
   `-var-create - * "name"`.  Multiple fixed varobjs for the same name at
   different addresses can coexist safely — each is permanently bound to its
   own stack slot.

2. **`variable.depth > min_depth_by_name[name]`** → this is an outer
   variable that cannot be reached by name. Create a **placeholder**
   (displays `name <address>`, non-expandable, no value tracking).

## Fixed Varobj Binding

Varobjs created with `- *` (fixed frame) are permanently bound at creation
time:

- GDB performs name lookup once at creation, resolving to a specific stack
  slot in a specific frame.
- Subsequent `-var-update` does NOT re-lookup by name — it tracks the
  original binding.
- If the frame is popped, the varobj reports `out_of_scope`.
- If another variable with the same name enters scope (shadowing), the
  existing varobj still tracks the original variable.

This means it is safe to create a name-based varobj for the innermost
variable even if a shadowing relationship later changes.

## Placeholder Lifecycle

### Creation

A placeholder is created when:

- The variable's type needs name fallback (`(anonymous namespace)` in type)
- The variable does NOT have the smallest depth for its name

### Promotion

A placeholder is promoted to a real varobj when:

- The placeholder's `(name, addr)` matches a variable in the current
  `get_locals_b64()` result
- That variable has the **smallest depth** among all same-named
  unparseable-type variables

After promotion, the promoted key is filtered out of the `to_add` list to
prevent duplicate node creation.

### Removal

Placeholders are removed when their `(name, addr)` key no longer appears
in the current variable list (the variable went out of scope).

## Reanchor Handling

When an existing varobj is flagged for reanchoring (shadowing state changed),
and its type is unparseable (anonymous namespace):

- The varobj is **left untouched** — its fixed binding is permanently valid
  regardless of current shadowing state.
- `_build_reanchor_bindings` skips entries whose `_varobj_type` contains
  `(anonymous namespace)`.
- This avoids deleting a perfectly valid fixed varobj and recreating it as a
  placeholder unnecessarily.

## Example Walkthrough

```cpp
namespace { struct A { int x; }; }

void foo() {
    A a{};          // depth=1 when inner scope is active
    int b = 10;
    {
        A a{};      // depth=0 (innermost)
        int b = 10;
    }
    int c = 10;     // after inner scope exits, outer a is depth=0
}
```

### Stepping to inner `int b = 10` (line with both `a`'s visible):

1. Variables from `get_locals_b64()` in original order: `a` at depth=0,
   `b` at depth=0, `a` at depth=1, `b` at depth=1
2. `min_depth_by_name["a"] = 0` (precomputed across all bindings)
3. Processing in original order — inner `a` (depth=0): matches min depth →
   `-var-create - * "a"` → success, binds to inner `a`
4. Outer `a` (depth=1): depth > min → **placeholder**

### Stepping past `}` (inner scope exits, only outer `a` visible):

1. Variables: `a` at depth=0, `b` at depth=0, `c` at depth=0
2. Inner `a`'s key is gone → removed from tracked
3. `_promote_placeholders`: outer `a` placeholder has depth=0 which equals
   `min_depth_by_name["a"]`, no same-named varobj exists → **promoted** via
   `-var-create - * "a"` → binds correctly to outer `a`

## Implementation Files

- `tgdb/local_variable_pane/reconcile.py` — `_type_needs_name_fallback()`,
  `_add_binding_by_name_fallback()`, `_promote_placeholders()`,
  `_reanchor_var()` unparseable-type handling
- `tgdb/gdb_controller/types.py` — `LocalVariable.depth` field
- `tgdb/gdb_controller/varobj.py` — wiring `depth` in `_publish_locals_async`
- `tgdb/tgdb_pysetup.py` — `depth` counter in `get_locals_b64()`
