# Known GDB Bugs

Catalogue of GDB defects that tgdb has hit in practice, with self-contained
reproducers.  Each entry is independent — add new ones by appending a new
`## Bug N: ...` section.

For every bug we record:

- **Symptom** — what the user sees.
- **Trigger** — the exact MI sequence that provokes it.
- **Crash signature** — the assert / error string GDB emits.
- **Reproducer** — a standalone script under
  [`../known_gdb_bug_reproduce/`](../known_gdb_bug_reproduce/), one
  file per bug, depending only on `gdb`, `g++`, and the Python
  standard library.  `## Bug N` in this document maps to
  `bug_N.py` on disk.  Run with `python3 known_gdb_bug_reproduce/bug_N.py`;
  exit code 0 means the bug is reproduced on the current GDB build,
  1 means it is not (assume fixed upstream or behaviour-different
  build), 2 means `gdb`/`g++` missing, 3 means setup diverged.
- **Affected GDB / libstdc++ versions** — what we have observed.
- **tgdb workaround** — commit + file references for the workaround logic
  in this repo, plus a note on the limit of the workaround.

---

## Bug 1: dynamic varobj children corrupted across `std::variant` alternative switch

### Symptom

When a `std::variant` is assigned to a different alternative under fixed
storage (e.g. `v = std::vector<int>{1,2,3}` after `v = std::string{"string"}`),
a follow-up `-var-list-children` on the parent varobj crashes the entire
GDB process.  This kills the whole debug session — `tgdb`, `cgdb`, and
plain `gdb -i=mi` all see GDB exit.

### Trigger

1. `-var-create` on a `std::variant<string, vector<int>, ...>` while the
   variant holds the string alternative.
2. `-var-list-children` to register the printer-synthesized child
   `varN.[contained value]`.
3. Step / continue past an assignment that flips the variant's active
   alternative to a vector-like (or otherwise array-indexed) type.
4. `-var-update` on the parent reports `new_num_children=N` plus a
   `new_children` list of `[1],[2],...`.
5. Any subsequent `-var-list-children` (or `-var-update` on the stale
   `[contained value]` child) trips the assertion.

The root cause is that step 4's payload covers only the *new* indices.
GDB keeps the stale `[contained value]` child in its varobj cache as the
implicit "child 0" of the new array layout — but the printer at index 0
of the new layout produces an `int`, not a string.  Step 5's value
refresh then trips an internal-consistency assert.

### Crash signature

Two related asserts in `gdb/varobj.c` (line numbers vary by build):

```
varobj.c:1298: internal-error: install_new_value:
  Assertion `!var->value->lazy ()' failed.

varobj.c:1301: internal-error: install_new_value:
  Assertion `!var->print_value.empty () && !print_value.empty ()' failed.
```

GDB prints `Quit this debugging session?` non-interactively, answers it
itself, and exits.

### Affected versions

Confirmed on Ubuntu's GDB with libstdc++ shipped on Ubuntu 24.04 LTS
(reproduced 2026-05-08).  The crash affects the `std::variant` /
`std::optional` / `std::any` family — any libstdc++ pretty printer that
exposes a single `[contained value]` child while the wrapped type is
"single-value-shaped" and switches to a multi-child shape (or vice
versa) under the same parent varobj.  `displayhint` does NOT change
across the transition for `std::variant` (it stays `"array"`), so
heuristics keying off `displayhint` cannot detect it.

### tgdb workaround

Detect the shape transition heuristically before issuing the next
child-walking MI command, drop the affected varobj on the GDB side,
and re-create it from scratch with a fresh printer state.  The fresh
varobj has clean cache state so subsequent `-var-list-children` is
safe.

Implementation across three commits:

- `ca5b9f1` — initial four-bug batch including a first cut at the
  shape-transition guard (gated on `displayhint` change, which turned
  out not to fire for `std::variant`).
- `ac63c14` — stop iterating dynamic-child varobjs in `_do_var_update`
  so the stale `[contained value]` child is never the target of a
  `-var-update`.
- `5d1b467` — heuristic-based detection in
  `tgdb/local_variable_pane/reconcile.py::_is_dynamic_shape_transition`
  (walks tracked child exps; flags a transition when any tracked exp
  is outside `{[0]..[N-1]}` for the new layout).  Plus
  `_readd_shape_transitioned_bindings` to recreate the varobj inside
  the same reconciliation pass so the user does not see the variable
  disappear from the locals pane.

Limit of the workaround: tgdb still recreates the varobj, so the
user's tree-expansion state for the variant's children is lost across
the transition.  This is a UX cost, not correctness — the alternative
(walking the corrupted varobj) crashes GDB.

### Reproducer

Run [`../known_gdb_bug_reproduce/bug_1.py`](../known_gdb_bug_reproduce/bug_1.py).
Exit code 0 = bug reproduced (GDB asserted / exited), 1 = not reproduced,
2 = `gdb`/`g++` not in PATH, 3 = setup diverged.


### Sample successful reproduction

```
[1] var-create  (done): 7^done,name="vv",numchild="0",
    value="std::variant [index 0]",
    type="std::variant<std::__cxx11::basic_string<...>, std::vector<int, ...> >",
    thread-id="1",displayhint="array",dynamic="1",has_more="1"

[2] var-list-children (string state) (done): 8^done,numchild="1",
    displayhint="array",
    children=[child={name="vv.[contained value]",
                     exp="[contained value]",numchild="0",
                     value="\"string\"",
                     type="std::__cxx11::basic_string<...>",
                     thread-id="1",displayhint="string",dynamic="1"}],
    has_more="0"

[3] var-update (after vector assignment) (done): 10^done,
    changelist=[{name="vv",
                 value="std::variant [index 1] containing std::vector of length 3, capacity 3",
                 in_scope="true",type_changed="false",
                 new_num_children="3",
                 displayhint="array",dynamic="1",has_more="0",
                 new_children=[{name="vv.[1]",exp="[1]",numchild="0",value="2",type="int"},
                               {name="vv.[2]",exp="[2]",numchild="0",value="3",type="int"}]}]

[4] var-list-children (post-transition) (exit):

CONFIRMED: GDB internal-error reproduced.
  -> varobj.c:1301: internal-error: install_new_value:
       Assertion `!var->print_value.empty () && !print_value.empty ()' failed.
  GDB process alive: False
```

Note the asymmetry in step 3 — `new_num_children=3` but `new_children`
only enumerates indices `[1]` and `[2]`.  Index `[0]` is implicitly the
"kept" child, and that child is still GDB's stale
`vv.[contained value]` from step 2.  Step 4 is what trips the assert.

---

## Bug 2: `cplus_describe_child: Assertion 'access' failed` on `-var-update` of certain class members

### Symptom

GDB aborts mid-session with an assert in `gdb/c-varobj.c::cplus_describe_child`
when tgdb issues `-var-update` on a child varobj that is a *grandchild
or deeper descendant of a dynamic-printer varobj* — typically a field
inside a `std::vector<std::vector<...>>`, `std::map`-of-struct, or
similar nested container in a class with public/private members.

The whole debug session ends; the user sees the locals pane freeze
and tgdb log a `GDB process exited` warning.

### Trigger

1. tgdb has tracked a varobj path like
   `var36.public.index.[N]`, where:
   - `var36` is a non-dynamic class/struct varobj,
   - `var36.public` is the access-specifier synthetic child,
   - `var36.public.index` is a member that GDB marked
     `dynamic="1"` (it has a libstdc++ pretty printer, e.g. it is a
     `std::vector<...>`),
   - `var36.public.index.[N]` is the N-th child synthesized by that
     printer.
2. At the next stop, tgdb's locals refresh issues `-var-update` on
   each tracked child individually:
   ```
   2161-var-update --all-values "var36.public.index.[0]"   ^done
   2162-var-update --all-values "var36.public.index.[1]"   ^done value=""
   2163-var-update --all-values "var36.public.index.[2]"
       <crash>
   ```
3. On certain class shapes (still narrowing what exactly), GDB enters
   `cplus_describe_child` to materialise access info for the
   sub-child it is about to refresh, finds that `access` is not
   determinable, and asserts.

### Crash signature

```
c-varobj.c:860: internal-error: cplus_describe_child:
  Assertion `access' failed.
```

(line number varies by GDB build).  Followed by the standard

```
A problem internal to GDB has been detected, further debugging may
prove unreliable.

Quit this debugging session? (y or n) [answered Y; ...]
Create a core file of GDB? (y or n) [answered Y; ...]
```

…and process exit.  tgdb logs `WARNING tgdb.locals: Skipped varobj
<name> during update: GDB process exited` for the offending varobj.

### Affected versions

Confirmed on the same Ubuntu 24.04 GDB / libstdc++ build that hits
Bug 1.  First seen 2026-05-09 against a real workload (the user's
in-house server source).  Reduced standalone reproducer landed
2026-05-09 once we identified the trigger pattern — see the
**Reproducer** section below.  The crash is deterministic on the
affected GDB build: 2/2 element children update cleanly, the
3rd lands the assertion every time.

### Related: timeout symptoms before the crash hits

Earlier in `tgdb.bug.log` (2026-05-04 03:33:53), the same
class-member iteration pattern produced a different visible
symptom: every `-var-evaluate-expression varN.public.<field>` and
`-var-update varN` timed out at 5–10 s, logging

```
WARNING tgdb.locals: var_evaluate_expression varN.public.<field>
    failed: MI command timed out — GDB may be busy
WARNING tgdb.locals: Skipped varobj varN during update:
    MI command timed out — GDB may be busy
```

GDB did not crash *that* session, but its varobj layer fell into
a wedged state.  The same code path that produces the timeouts
sometimes lands the assert directly; both come from issuing
`-var-update` against a child of a dynamic varobj.

### tgdb workaround

Commit `adb9e13` extends `tgdb/local_variable_pane/update.py
::_build_safe_to_update_varobjs` to skip every varobj that is a
descendant of any entry in `_dynamic_varobjs` — not just
descendants of dynamic *roots*.  Children of non-root dynamic
varobjs (the `var36.public.index.[N]` family above) are now
refreshed exclusively through their dynamic ancestor's
`-var-update` cascade, so tgdb never issues an MI command that
can land in `cplus_describe_child` on a descendant of a printer-
synthesized subtree.

Limit of the workaround: this prevents tgdb from triggering the
crash via the locals refresh.  Manual `-var-update` issued from
the command pane (e.g. `interpreter-exec mi "-var-update ..."`)
on such a child can still hit it — that is on the user.

### Reproducer

Run [`../known_gdb_bug_reproduce/bug_2.py`](../known_gdb_bug_reproduce/bug_2.py).
Exit code 0 = bug reproduced (GDB asserted / exited), 1 = not reproduced,
2 = `gdb`/`g++` not in PATH, 3 = setup diverged.

The reproducer synthesises a minimal class:

```cpp
struct Container
{
    int mask;
    int connector;
    int offset;
    std::vector<std::vector<long>> index;
};
```

with three populated `index` entries (lengths 3, 0, 2), then walks the
locals-pane MI sequence that tgdb itself used to issue:
``-var-create``, recurse `-var-list-children` down through the synthetic
`.public` access node into `index`, then `-var-update` each
``vv.public.index.[N]`` element child sequentially.  On the affected
GDB build the third such update lands the assertion
deterministically (`[0]` and `[1]` succeed, `[2]` crashes).


---

## Bug 3: `block.__iter__()` yields duplicate symbols from parent-block merging of sibling scopes

### Symptom

The locals pane shows every variable twice.  When navigating frames with
`up`/`down` to a large constructor or function that contains multiple
sibling `{ }` blocks (each declaring identically-named variables like
`parms` and `result` for successive RPC calls), GDB's Python
`block.__iter__()` yields the same variable names multiple times from
the function-body block.  Debugger front-ends that build a locals pane
from this iterator see phantom duplicates.

Two distinct sub-issues are observed within a single block walk:

1. **Cross-depth duplication** — a variable such as `numConnIds` appears
   at depth 0 (the innermost scope block where the PC sits) with a real
   stack address, AND again at depth 1 (the function-body block) with
   `val.address == None`.  The depth-1 copy is a phantom: the same
   declaration line, same block start/end, but no dereferenceable address.

2. **Sibling-scope merging** — variables `parms` and `result`, each
   declared inside distinct `{ }` blocks (sibling scopes at the same
   nesting level), ALL appear in the function-body block at depth 1 with
   their original declaration lines — even though only the currently
   executing block's variables should be visible.  The block walk follows
   the `superblock` chain (inner → outer) and never visits sibling blocks,
   so these symbols can only come from GDB merging child-block symbols
   into the parent.

### Trigger

The trigger requires a constructor (or large function) with:

- Lambda-initialised variables (`const auto x = [...]() { ... }();`)
- Multiple `{ }` blocks that each declare identically-named local variables
  (e.g. `parms`, `result` for different RPC-like calls)
- The debugger stops inside a callee called from near the end of the
  constructor, and the user uses `up` to navigate to the constructor frame

Reproduced at **all optimization levels** (`-O0`, `-O1`, `-O2`, `-O3`)
with `-g3` debug info.

### Affected versions

Confirmed on:
- GDB 15.0.50.20240403-git (Ubuntu 24.04 LTS)
- g++ 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04)

The bug is likely present in all GDB versions that implement the Python
`gdb.Block.__iter__()` API, since the root cause is in how GDB
populates the function-body block's symbol table from DWARF child-scope
entries.

### tgdb workaround

Three layers of defence in `tgdb/tgdb_pysetup.py::_collect_locals()`:

1. **Skip shadowed register copies** (commit `1e0fc63`): when a variable
   is both shadowed (same name already seen at a shallower depth) AND has
   a synthetic `register@N` address (no real stack address), skip it.
   This handles the cross-depth duplication — the depth-0 copy with a
   real address is kept, the depth-1 phantom is dropped.

2. **Deduplicate same-key entries** (commit `ad333d3`): after collecting
   all variables, group by `(name, addr)`.  When multiple entries share
   the same key, keep the first one with a real value; fall back to the
   first entry if all are `<optimized out>`.  This handles the
   sibling-scope merging and the within-block DWARF duplicate entries.

3. **varobj guard** (commit `da3a23c`): the `use_addr` check in
   `tgdb/local_variable_pane/reconcile.py` rejects any address starting
   with `register@` so the reconciliation engine does not attempt to
   create MI varobjs with invalid expressions like
   `*(type*)register@depth1`.

Limit of the workaround: genuine shadowed variables from nested scopes
(e.g. an inner block intentionally re-declaring the same name) that
happen to be register-allocated will be hidden.  This is acceptable
because GDB's merged parent-block copies are the common case; true
C++ variable shadowing with register allocation is rare.

### Reproducer

Run [`../known_gdb_bug_reproduce/bug_3.py`](../known_gdb_bug_reproduce/bug_3.py).
Exit code 0 = bug reproduced, 1 = not reproduced,
2 = `gdb`/`g++` not in PATH, 3 = setup diverged.

### Sample successful reproduction

```
Total symbols: 36
Names appearing more than once: 15

=== CROSS-DEPTH DUPLICATION ===
Variables present in both inner block (real addr) and
function-body block (addr=None):

  currRunDir                depth=0 line= 46 has_addr=True
  currRunDir                depth=1 line= 46 has_addr=False
  numConnIds                depth=0 line= 62 has_addr=True
  numConnIds                depth=1 line= 62 has_addr=False
  ...13 more variables...

=== SIBLING-SCOPE MERGING ===
Same name at same depth but different declaration lines
(from sibling { } blocks merged into function body):

  parms                     depth=1 lines=[72, 79]
  result                    depth=1 lines=[73, 80]

CONFIRMED: GDB block iterator produces duplicate/merged symbols.
```

Note: the depth-0 entries have `has_addr=True` (real stack addresses)
while the depth-1 copies of the same variables have `has_addr=False`
(register-allocated phantoms).  The `parms` and `result` entries at
depth 1 show different declaration lines — these come from two separate
`{ }` blocks in the source, merged by GDB into the function-body block.
