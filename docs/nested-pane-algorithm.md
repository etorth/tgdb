# Nested Pane Layout Algorithm

How tgdb's workspace is split, resized, and mutated. This is the
single source of truth — `cfg.winsplit` and `cfg.winsplitorientation`
no longer exist; there is no special "two-pane cgdb mode" anywhere
in the codebase.

---

## Conceptual model

The workspace is a tree of `PaneContainer` widgets.

- A `PaneContainer` has an `orientation` (`"horizontal"` or `"vertical"`)
  and a list of `items`.
- Each item is either a leaf pane widget (`SourceView`, `GDBWidget`,
  `LocalVariablePane`, `EmptyPane`, …) or another `PaneContainer`.
- `orientation == "horizontal"` flows children **side-by-side**
  (vertical divider lines between columns). `orientation == "vertical"`
  stacks children **top-to-bottom** (horizontal divider lines
  between rows). This matches CSS `layout: horizontal | vertical`
  and Textual's same-named flow values.

```
                root  (#split-container)
                  │
       ┌──────────┼──────────┐
       │          │          │
   sub-cont     LOCALS    sub-cont
  (vertical)              (horizontal)
       │                      │
    ┌──┴──┐                ┌──┴──┐
  SOURCE  GDB           STACK  REGS
```

There is no upper bound on nesting depth; the tree shape is
entirely driven by user actions.

### Addresses

Every cell has an **address**: a `list[int]` of child indices
starting at the root container.

| Address      | Meaning                                            |
| ------------ | -------------------------------------------------- |
| `[]`         | The root container itself (used for splits)        |
| `[0]`        | First child of root                                |
| `[1, 0]`     | First child of the second child of root            |
| `[2, 1, 3]`  | Nested three levels deep                           |

Addresses are resolved lazily against the live widget tree at the
moment an async operation fires — it is safe to build a
`PaneHandle` before the cell it references has been created.

### Sizes — weights and CSS

For each child of a `PaneContainer`, the container stores an
integer **weight** in `_weights`. When rendering, weights are
written to the child's CSS along the orientation axis:

```
orientation == "horizontal":  child.styles.width  = "{weight}fr"
                              child.styles.height = "1fr"
orientation == "vertical":    child.styles.width  = "1fr"
                              child.styles.height = "{weight}fr"
```

Weights are integers — captured from the children's current
laid-out pixel sizes whenever the user drags or invokes
`resize_first_child` (see below). Textual's `fr` unit handles
the proportional distribution.

### Splitters

In a **horizontal** container, a one-cell `Splitter` widget sits
between every adjacent item pair (visible as a vertical bar).
The splitter is draggable and produces `DragResize` messages.

In a **vertical** container, no splitter widget is inserted; the
title bar of the lower pane (drawn by `PaneBase`) doubles as the
boundary and dispatches `_resize_from_title_drag` directly.

---

## Startup sequence

The order tgdb settles into a working layout:

1. **GDB sources `~/.gdbinit`.** Out of tgdb's control.
2. **GDB runs `tgdb_pysetup.py`.** Wires the socket bridge for
   data collection. Workspace is untouched.
3. **tgdb runs `$XDG_CONFIG_HOME/tgdb/tgdbrc`** (or the path
   passed via `--rc`). The rc file can mutate the workspace
   directly through the `tgdb.screen` API (typically inside a
   `python << EOF … EOF` block).
4. **Default fallback.** If the root container is still empty
   after the rc file finishes, `core.py::_install_default_layout_if_empty`
   installs the classic two-pane layout: `SourceView` on top,
   `GDBWidget` on bottom (root `orientation = "vertical"`).
5. **GDB prompt is ready** for user input.

The default fallback is *unconditional* on rc-file presence —
even when the user passes `--rc NONE` or has no rc file at all,
step 4 still runs so they always get a working initial view.

### Why root starts empty

Earlier versions pre-populated the root with `[source, gdb]` in
`on_mount`, then forced `set_orientation(cfg.winsplitorientation)`
after each `_sync_config` tick. The rc-file Python could mutate
the workspace, then `_sync_config` would immediately revert the
root orientation back to the cfg default, clobbering the user's
layout.

The startup now leaves the root empty, runs the rc file, and
only installs the default if the user didn't customize. There
is no `cfg.winsplitorientation`, no `_apply_split`, no orientation
forcing.

---

## Python API — `import tgdb`

Defined in `tgdb/tgdb_api.py`. Each public coroutine logs at
INFO level on entry (logger name `tgdb.api`).

### `tgdb.SplitMode`

```python
class SplitMode(enum.Enum):
    HORIZONTAL = "horizontal"  # left/right  (vertical divider)
    VERTICAL   = "vertical"    # top/bottom  (horizontal divider)
```

### `tgdb.Pane`

```python
class Pane(enum.Enum):
    SOURCE     = "source"
    GDB        = "gdb"
    LOCALS     = "locals"
    REGISTERS  = "registers"
    STACK      = "stack"
    THREADS    = "threads"
    EVALUATE   = "evaluate"
    MEMORY     = "memory"   # multi-instance
    DISASM     = "disasm"
```

All `Pane` values except `MEMORY` are singletons: the same widget
is returned regardless of how many times `attach` is called for
that kind. Attaching a singleton to a new slot detaches it from
its previous slot first.

### `tgdb.screen` — workspace API

```python
# Read-only (sync)
tgdb.screen.size()                         # → (width, height) in cells
tgdb.screen.width()
tgdb.screen.height()
tgdb.screen.get_pane(address)              # → PaneHandle
```

```python
# Mutating (async — must be awaited)
await tgdb.screen.close_all_panes()
await tgdb.screen.split(pane=address, mode=SplitMode.HORIZONTAL)
await tgdb.screen.close(address)
await tgdb.screen.get_pane(address).attach(Pane.SOURCE)
await tgdb.screen.get_pane(address).detach()
```

`split(pane=[], mode=...)` always operates on the root container:
it ensures the root has the requested orientation, then appends
one new `EmptyPane`.

`split(pane=address, mode=...)` operates on the cell at
*address*:

- If *address* resolves to a `PaneContainer`, the new empty cell
  is added inside it (orientation adjusted if needed).
- Otherwise (a leaf pane), the cell is wrapped in a fresh
  `PaneContainer` of the requested orientation and a new empty
  cell is added alongside it.

### Worked example

The rc-file recipe for source-on-top-left / gdb-on-bottom-left /
locals-on-the-right:

```python
python << EOF
await tgdb.screen.close_all_panes()

await tgdb.screen.split(pane=[],  mode=tgdb.SplitMode.HORIZONTAL)
await tgdb.screen.split(pane=[0], mode=tgdb.SplitMode.VERTICAL)

await tgdb.screen.get_pane([0, 0]).attach(tgdb.Pane.SOURCE)
await tgdb.screen.get_pane([0, 1]).attach(tgdb.Pane.GDB)
await tgdb.screen.get_pane([1]   ).attach(tgdb.Pane.LOCALS)
EOF
```

Resulting tree:

```
root  (horizontal)
├── sub-container  (vertical)        ← [0]
│   ├── SOURCE                       ← [0, 0]
│   └── GDB                          ← [0, 1]
└── LOCALS                           ← [1]
```

---

## Context menu

The same mutations are reachable via the right-click context
menu (`tgdb/context_menu/`) — handlers in
`tgdb/workspace_actions.py`. They produce the same tree shapes
as the Python API but route through `_apply_context_menu_action`,
`_add_pane_to_workspace`, `_hide_workspace_item`,
`_delete_workspace_item`. The Python API is the recommended
surface for scripted setups; the menu is for interactive use.

---

## Keyboard shortcuts (root container)

While focus is in the source pane in TGDB mode, four shortcuts
act on the **root container**:

| Key      | Effect                                                 |
| -------- | ------------------------------------------------------ |
| `=`      | Grow root's first child by 1 cell along the axis       |
| `-`      | Shrink root's first child by 1 cell                    |
| `+`      | Grow root's first child by ~25% of the root's axis     |
| `_`      | Shrink root's first child by ~25%                      |
| `Ctrl+W` | Toggle the root container's orientation                |

These are routed via `ResizeSource` / `ToggleOrientation`
messages defined in `tgdb/source_widget/messages.py` and handled
in `tgdb/layout.py`.

Requirements:

- Root must have **at least 2 items** (with one item there is
  nothing to resize / nothing distinct to toggle).
- For resize keys, the root's axis size must be > 0 (skipped
  during the first frame before layout has run).

The "first child" is `root.items[0]`. To act on a different cell,
use the mouse drag-resize on the splitter / title bar between
that cell and its neighbours, or call `resize_first_child` on
the appropriate nested container from a `:python` block.

---

## Resize algorithms

### `PaneContainer.resize_first_child(delta: int) → bool`

Used by the `=`/`-`/`+`/`_` keys at the root, but generic — any
container can resize its first item from a script.

Algorithm:

1. Capture the children's current laid-out sizes
   (`item.size.width` or `item.size.height` depending on
   orientation). If any size is zero, refuse (layout hasn't run
   yet).
2. Clamp the new first-child size to
   `[min_size, total − (N−1)·min_size]`. `min_size` is
   `min_item_width = 4` or `min_item_height = 2` depending on
   orientation; `N` is the item count.
3. If the clamp left the new size unchanged, return False
   (refused).
4. Distribute `total − new_first` among items `1..N-1`
   proportionally to their **current** sizes:
   ```
   share_i  =  (total − new_first) · (size_i / sum(size_1..N-1))
   ```
5. Round each share to an integer and apply rounding drift to the
   largest of the trailing items so the chosen first-child size
   is preserved exactly.
6. Refuse if any trailing item falls below `min_size`.
7. Write the resulting list into `_weights`, call
   `_apply_orientation` (which writes the new `fr` values into
   each child's CSS), and `refresh(layout=True)`.

Important property: **relative ratios among the trailing items
stay constant**. If before the call children 2 and 3 were 25% and
50% of the parent (i.e. 1:2), after the call they are still in
1:2 ratio of whatever remains.

Example with root height 100 and three children at sizes 25 / 25 / 50:

```
press +  (delta = +25, i.e. ~25% of 100)
  first_new   = clamp(25 + 25, [2, 100 − 2·2]) = 50
  remaining   = 100 − 50 = 50
  share_2     = 50 · (25 / 75) ≈ 16.7  → 17
  share_3     = 50 · (50 / 75) ≈ 33.3  → 33
  drift fix   = 50 − (17 + 33) = 0     (no adjustment)
  → 50 / 17 / 33      (first child is now 50%, others keep 1:2)
```

### Mouse drag — splitter (`PaneContainer._resize_from_drag`)

Triggered by a `DragResize` message from a `Splitter` widget
in a **horizontal** container.

1. Find the two items immediately surrounding the dragged
   splitter via `_adjacent_items`.
2. Capture current weights from live laid-out sizes.
3. Compute `total = before_size + after_size`.
4. Translate the pointer position into `new_before`, clamped to
   `[min_size, total − min_size]`.
5. `new_after = total − new_before`.
6. Update only those two weights; leave every other child
   untouched.
7. Apply and refresh.

Only the two adjacent items participate; the rest of the
container's children keep their existing weights, so the rest
of the layout is unaffected.

### Mouse drag — title bar (`PaneContainer._resize_from_title_drag`)

Triggered when a `PaneBase` title bar is dragged in a **vertical**
container (since vertical containers have no splitter widgets).
Same algorithm as splitter drag, but applied to the pair
`(before, after)` chosen by the title bar's owner (which is the
"after" pane — its title acts as the visual top boundary).

Posts a `TitleBarResized` message so any container-level
observers can react.

---

## Orientation toggle

`PaneContainer.set_orientation_async(orientation)`:

1. No-op if `orientation` already matches.
2. Update `self.orientation`.
3. `await self._rebuild()` — re-create the DOM children:
   - For horizontal, insert a `Splitter` between each adjacent
     pair.
   - For vertical, no splitters (title bars suffice).
   - Update `self.styles.layout = orientation` so Textual's CSS
     flow matches.

There is a synchronous `set_orientation` variant that defers the
rebuild to `call_later` for callers that can't await. **Don't
use it** when the caller is about to perform further DOM
mutations on the same container — the deferred rebuild will run
during one of the caller's `await` points and tear out
widgets the caller just mounted (`MountError: … has no parent`).
The async variant exists precisely to make this safe.

The Ctrl+W shortcut always uses the async variant.

---

## Empty cells (`EmptyPane`)

Defined in `tgdb/workspace.py`. Used as a placeholder in any
slot that does not currently host a real pane:

- `split` creates one in the new cell.
- `detach` replaces a pane with one.
- `close_all_panes` resets the root to `[EmptyPane]`.
- After deleting a singleton pane, the slot the user previously
  saw it in is replaced with an `EmptyPane` so the surrounding
  layout doesn't collapse.

`EmptyPane` renders as a tinted blank cell. It has no behaviour
other than being a target for the next attach.

---

## Lifecycle helpers (`workspace_actions.py`)

Internal helpers called by the context menu and by the public
API. None of them are part of the user-facing Python surface.

| Helper                              | Purpose                                                |
| ----------------------------------- | ------------------------------------------------------ |
| `_ensure_dynamic_workspace`         | Return the root container (no-op shim today)           |
| `_replace_workspace_item`           | Replace a leaf via its parent container                |
| `_normalize_container_after_delete` | Collapse 1-item / 0-item parents up the tree           |
| `_add_pane_to_workspace`            | Attach a pane next to a target                         |
| `_hide_workspace_item`              | Replace a leaf with `EmptyPane`                        |
| `_delete_workspace_item`            | Remove a leaf, then normalize                          |
| `_apply_context_menu_action`        | Split a leaf in a given direction (left/right/up/down) |

`_normalize_container_after_delete` walks up the ancestor chain
collapsing any container that ends up with 0 or 1 items: a
1-item container is replaced with its single child, a 0-item
container is replaced with an `EmptyPane`. This prevents
orphaned containers from accumulating in the tree.

---

## Logging

Everything mutating the layout emits an INFO line:

- `tgdb.api` — every public `tgdb.screen.*` / `PaneHandle.*` call
  (see `tgdb_api.py`).
- `tgdb.workspace` — pane creation (`creating pane: <kind>`) and
  internal mutations.
- `tgdb.config` — every rc-file line and interactive command at
  `execute: '…'`.

A typical successful rc-file run looks like:

```
tgdb.config:  loading rc file: /home/.../tgdbrc (14 lines)
tgdb.config:  execute: 'set tabstop=4'
tgdb.config:  execute: 'python' (+8 more lines)
tgdb.api:     api: close_all_panes()
tgdb.api:     api: split(pane=[], mode=horizontal)
tgdb.api:     api: split(pane=[0], mode=vertical)
tgdb.api:     api: attach(address=[0, 0], pane=source)
tgdb.workspace: creating pane: source
tgdb.api:     api: attach(address=[0, 1], pane=gdb)
tgdb.workspace: creating pane: gdb
tgdb.api:     api: attach(address=[1], pane=locals)
tgdb.workspace: creating pane: locals
```

This is the single most useful trace when debugging a layout
that came out wrong.

---

## Constraints and limits

- **Minimum item size.** `min_item_width = 4`, `min_item_height = 2`.
  Drag-resize and `resize_first_child` refuse to violate these.
- **No persistence.** The layout is purely runtime state. tgdb
  does not write the workspace tree to disk; users reproduce
  their preferred layout by scripting it in the rc-file Python
  block.
- **Singletons** of `SourceView`, `GDBWidget`, and all other
  non-`MEMORY` pane kinds are unique per app instance. Attaching
  a singleton to a new slot detaches it from the old slot first
  (see `tgdb_api.py::_do_attach`).
- **Memory panes** are multi-instance — every `attach(Pane.MEMORY)`
  creates a fresh `MemoryPane` widget.
- **Refusal is silent.** `resize_first_child` returning `False`
  is intentional and is not surfaced to the user. A failed drag
  manifests as the boundary simply not moving.

---

## Files

| File                                | Role                                                       |
| ----------------------------------- | ---------------------------------------------------------- |
| `tgdb/workspace.py`                 | `PaneContainer`, `Splitter`, `EmptyPane`, drag handlers    |
| `tgdb/pane_base.py`                 | `PaneBase` (title bar, mouse handling for nested drag)     |
| `tgdb/workspace_actions.py`         | Pane factories + context-menu action handlers              |
| `tgdb/layout.py`                    | Root-level keyboard shortcuts (`=`/`-`/`+`/`_`/Ctrl+W)     |
| `tgdb/tgdb_api.py`                  | Python API (`tgdb.screen`, `tgdb.Pane`, `tgdb.SplitMode`)  |
| `tgdb/core.py`                      | `compose` + default-layout install                         |
| `tgdb/source_widget/messages.py`    | `ResizeSource`, `ToggleOrientation` message types          |
| `tgdb/source_widget/keys.py`        | Posts the messages on `=`/`-`/`+`/`_`/Ctrl+W keypresses    |
