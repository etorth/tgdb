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

## Data Source: `_collect_locals()` via Pipe

`_publish_locals_async()` invokes the GDB Python convenience function
registered by `tgdb_pysetup.py`.  The function `_collect_locals()` runs
inside GDB, walks the block tree, applies a multi-stage filtering and
deduplication pipeline, then writes the result as a JSON payload through
the data pipe (tag `l`) using the unified frame format
(`[tag][ctl][7B len][payload]`, see `docs/pipe-protocol.md`).

### Block Walk

Starting from `frame.block()` (the innermost scope containing the
current PC), the walk iterates every symbol in the block, then moves to
`block.superblock` (one level outward), incrementing the `depth` counter.
The walk stops when it reaches the function-body block
(`block.function is not None`) or there is no superblock.

```
depth 0 — innermost block (the { } the PC is inside)
depth 1 — next enclosing block
  ...
depth N — function-body block (stop here)
```

### Per-Symbol Fields

For each symbol the walk collects:

| Field          | Source / Meaning                                         |
|----------------|----------------------------------------------------------|
| `name`         | `symbol.name`                                            |
| `value`        | `_format_value(val)` — formatted with unlimited elements |
| `type`         | `str(symbol.type)` — declared type string                |
| `addr`         | hex address from `val.address`, or `"register@<depth>"`  |
| `depth`        | block depth (0 = innermost)                              |
| `line`         | `symbol.line` — declaration line number                  |
| `is_arg`       | `symbol.is_argument`                                     |
| `is_reference` | lvalue or rvalue reference                               |
| `ref_kind`     | `"lvalue (&)"`, `"rvalue (&&)"`, or `None`               |
| `is_shadowed`  | True if same name already collected at a shallower depth |
| `scope_start`  | `hex(block.start)` — block start address                 |

**Address resolution:**

- For references: `str(val.referenced_value().address)`.
- For non-references: `str(val.address)` if not None, else
  `"register@<depth>"` (variable lives in a register, not on the stack).
- On exception: `"unknown"`.

### Pre-Collection Filters

Before a symbol enters the collection list, three early filters apply:

1. **Non-variable skip**: only `symbol.is_variable` or `symbol.is_argument`.
2. **Builtin name skip**: compiler-generated names like `__for_begin`,
   `__for_end`, `__for_range` (C++ range-for lowering) and the scratch
   variable `_` are hidden.
3. **Not-yet-declared skip**: if `symbol.line >= current_line` (and the
   symbol is not an argument), skip it — the variable is declared after
   the current execution point and is not yet in scope.

### GDB Bug 3 Noise Filter (Cross-Depth Phantoms)

GDB's `block.__iter__()` merges child-block symbols into the parent
function-body block (see `docs/known_gdb_bug.md` § Bug 3).  This causes
a variable like `dccInterleave` to appear both at depth 0 with a real
stack address and value, and again at depth 1 with `val.address == None`
(mapped to `register@1`) and an `<optimized out>` value.

The filter detects these phantoms precisely:

```
if (name already seen at shallower depth)
   AND (current addr starts with "register@")
   AND (shallower copy has same declaration line)
   AND (shallower copy has same declared type)
   AND (shallower copy has a real stack address)
→ skip this symbol (it is a GDB-merged phantom)
```

This avoids false positives on genuine shadowed variables which have
different types or declaration lines (e.g. `parms` at line 390 with
type `CheckVersionParams` vs `parms` at line 421 with type
`OpenEngineParams` — those are real sibling-scope variables, not noise).

### Post-Collection Dedup (Same-Key Register Variables)

After the walk completes, a second deduplication pass handles symbols
that GDB yields **twice within the same block** — same `(name, addr)`
key.  This happens when the compiler splits a variable's lifetime into
multiple DWARF location ranges.

```
Group by (name, addr).
If a key appears more than once:
    keep the first entry that has a real value (not "<optimized out>");
    fall back to the first entry if all copies are optimized out.
```

Stack-allocated variables have unique hex addresses and never collide.
Register variables share synthetic `register@<depth>` addresses and are
the ones that produce duplicate keys.

### Output

The final deduplicated list is serialized as JSON and sent through the
data pipe as tag `l`.  tgdb receives it in `PipeDataMixin`, parses the
JSON, and calls `on_locals(variables)` to feed the reconciliation engine.

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

- `tgdb/tgdb_pysetup.py` — `_collect_locals()`: GDB-side Python function
  that walks the block tree, applies noise filters and dedup, writes
  JSON to the data pipe (tag `l`)
- `tgdb/gdb_controller/pipe_data.py` — `PipeDataMixin`: receives pipe
  frames, parses JSON, dispatches `on_locals()` callback
- `tgdb/gdb_controller/results.py` — `_handle_frame_result()`: triggers
  `_publish_locals_async()` on `kind="current-location"`
- `tgdb/gdb_controller/varobj.py` — `_publish_locals_async()`: invokes
  the GDB convenience function, builds `LocalVariable` list
- `tgdb/callbacks.py` — `_ui_on_cli_prompt()`: sends
  `request_current_location()` on every GDB prompt
- `tgdb/local_variable_pane/reconcile.py` — `_update_variables()`:
  incremental reconciliation logic (varobj lifecycle, tree diffing)
- `tgdb/local_variable_pane/update.py` — `_build_reanchor_bindings()`,
  `_build_removed_bindings()`, etc.
- `docs/known_gdb_bug.md` — Bug 3 documents the GDB block iterator
  duplicate/merge bug that the noise filter works around
- `docs/pipe-protocol.md` — Pipe frame format specification
