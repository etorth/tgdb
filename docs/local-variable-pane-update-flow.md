# Local Variable Pane Update Flow

## When the Locals Pane Refreshes

The locals pane refreshes through a **single unified path**:

```
GDB event → prompt notify pipe → request_current_location()
         → _handle_frame_result (kind="current-location")
         → _publish_locals_async()
         → on_locals(variables)
         → _update_variables()
```

This fires in two scenarios:

1. **Inferior stops** (`*stopped` record): GDB halts execution, then
   redisplays its prompt → the notify pipe fires `P\n` → tgdb sends
   `-stack-info-frame` → the response triggers `_publish_locals_async()`.

2. **Frame navigation** (CLI `up`/`down`/`frame N`): The user types a
   frame-change command in the GDB pane → GDB redisplays its prompt →
   same path as above.

Both cases converge at `_handle_frame_result` with
`meta["kind"] == "current-location"`.

## Data Source: get_locals_b64()

`_publish_locals_async()` calls `get_locals_b64()` — a GDB Python
convenience function registered by `tgdb_pysetup.py`. It walks the
block tree from innermost to outermost, collecting:

- `name`: variable name
- `value`: string representation from `str(val)`
- `type`: type string (e.g. `int`, `(anonymous namespace)::A`)
- `addr`: memory address or `"register"` / `"unknown"`
- `depth`: 0 = innermost block, increments outward
- `line`: declaration line number
- `is_shadowed`: True if a same-named variable at smaller depth exists
- `is_reference`: True for lvalue/rvalue references
- `is_arg`: True for function arguments

The returned list is in **block-walk order** (innermost first). No sort
is applied in `get_locals_b64()` — sorting happens locally where needed.

## Incremental Reconciliation (_update_variables)

The pane does NOT rebuild from scratch on each refresh. It performs an
incremental diff:

### Step 1: Compute Bindings

Each variable is keyed by `(name, addr)` — the **BindingKey**. This
uniquely identifies a variable instance across refreshes (same name at
same address = same variable).

### Step 2: Diff Against Current State

- **to_remove**: keys currently tracked but absent from new list
- **to_add**: keys in new list but not currently tracked
- **to_reanchor**: keys present in both but now shadowed (need address
  re-pinning for parseable types)

### Step 3: Fast Path (No Changes)

If nothing changed (`to_remove`, `to_add`, `to_reanchor` all empty,
same frame key), just run `-var-update *` to refresh existing values.

### Step 4: Update Existing Varobjs

Run `-var-update *` excluding stale varobjs (those being removed or
reanchored). This updates values for unchanged variables.

### Step 5: Reanchor Shadowed Variables

For variables that became shadowed and have parseable types, delete the
old floating varobj and create a new address-pinned one. Variables with
unparseable types (anonymous namespace) are **skipped** — their fixed
binding remains valid regardless of shadowing.

### Step 6: Remove Gone Variables

Delete varobjs and tree nodes for variables that left scope.

### Step 7: Promote Placeholders

Check if any existing placeholders can now be promoted to real varobjs
(their anonymous-namespace variable is now the innermost for its name).

### Step 8: Add New Variables

For each new variable:
- **Parseable type with address**: create varobj via `*(type*)addr`
- **Unparseable type (anonymous namespace), smallest depth**: create
  varobj by plain name (`-var-create - * "name"`)
- **Unparseable type, NOT smallest depth**: create placeholder (shows
  name and address, non-expandable)
- **Creation failure**: show value string as placeholder

### Step 9: Sort Root Children

After all additions, `_sort_root_children_by_line()` reorders root-level
tree nodes by their variable's declaration line. This ensures consistent
display order regardless of async creation timing or processing order.
Stable sort preserves original order for same-line variables.

### Step 10: Sync Shadow Labels

Update visual markers (dim/highlight) for shadowed vs active variables.

## Generation Counter (_rebuild_gen)

Every call to `_update_variables` increments `_rebuild_gen`. Each async
operation checks `if self._rebuild_gen != gen: return` — if a newer
refresh arrives mid-flight, the current one aborts. This prevents stale
results from overwriting fresher data.

## Display Order

Variables are displayed in **declaration line order** (smallest line
number at top, largest at bottom). Outer-scope variables typically have
smaller declaration line numbers (they are declared earlier in the
function body), so they tend to appear above inner-scope variables — but
this is a consequence of line ordering, not a guaranteed scope rule.
Same-line variables preserve the order from `get_locals_b64()` (which
matches GDB's symbol iteration order = left-to-right declaration).

## Expansion State Preservation

When switching frames, the pane saves expanded paths (which nodes were
open) keyed by `FrameKey`. When returning to a previously visited frame,
saved expansions are restored automatically.

## Implementation Files

- `tgdb/gdb_controller/results.py` — `_handle_frame_result()`: triggers
  `_publish_locals_async()` on `kind="current-location"`
- `tgdb/gdb_controller/varobj.py` — `_publish_locals_async()`: calls
  `get_locals_b64()`, builds `LocalVariable` list, fires `on_locals()`
- `tgdb/callbacks.py` — `_ui_on_cli_prompt()`: sends
  `request_current_location()` on every GDB prompt
- `tgdb/tgdb_pysetup.py` — `get_locals_b64()`: GDB Python function
  collecting variables from the block tree
- `tgdb/local_variable_pane/reconcile.py` — `_update_variables()`:
  incremental reconciliation logic
- `tgdb/local_variable_pane/update.py` — `_build_reanchor_bindings()`,
  `_build_removed_bindings()`, etc.
