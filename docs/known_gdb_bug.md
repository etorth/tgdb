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

## Bug 3: `call_function_by_hand_dummy` FSM assertion crash

### Symptom

GDB aborts with an internal error during a tgdb session.  The user sees
the debug session terminate abruptly; tgdb logs `GDB process exited`
warnings and the MI channel goes dead.

### Trigger

The crash is a dual-UI race condition.  tgdb creates a secondary MI
channel via `new-ui mi <pty>`.  GDB then has two active UIs:

- **UI-0 (primary console PTY)**: receives user keystrokes
- **UI-1 (new-ui MI channel)**: receives tgdb's MI commands

The race sequence observed in production (`tgdb.bug.log`):

1. The inferior is stopped at a frame with complex C++ locals
   (smart pointers, futures, lambdas — 107 locals total).
2. tgdb sends `-stack-list-variables --all-values` on the MI channel
   (UI-1).  For certain C++ types, GDB's value-printing path
   internally calls `call_function_by_hand` (e.g. for operator
   evaluation, xmethod dispatch, or pretty-printer callbacks).
3. Inside `call_function_by_hand_dummy`, GDB saves the current
   thread FSM, creates a new one (`sm`), and enters
   `run_inferior_call` → `wait_sync_command_done` → event loop.
4. Critically, `run_inferior_call` only unregisters the **current**
   UI's file handler (`current_ui->unregister_file_handler()`),
   leaving UI-0's input handler active in the event loop.
5. The user presses Enter on the primary console (repeating the
   last `next` command).  The event loop dispatches the console
   input handler → GDB executes `next` → `clear_proceed_status(1)`
   → `clear_proceed_status_thread()` iterates all threads in
   all-stop mode → `release_thread_fsm()` destroys `sm`.
6. Back in `call_function_by_hand_dummy`, the assertion
   `call_thread->thread_fsm() == sm` fails because `sm` has been
   released.

The race is timing-dependent: the console input must arrive and be
processed during the narrow window where `wait_sync_command_done` is
in its event loop.

### Crash signature

```
infcall.c:1594: internal-error: call_function_by_hand_dummy:
  Assertion `call_thread->thread_fsm () == sm' failed.
```

(line number varies by GDB build).  Followed by:

```
A problem internal to GDB has been detected, further debugging may
prove unreliable.

Quit this debugging session? (y or n) [answered Y; ...]
Create a core file of GDB? (y or n) [answered Y; ...]
```

### Affected versions

Confirmed on Ubuntu 24.04 GDB (likely 14.x / 15.x) with libstdc++
pretty-printers enabled.  First observed 2026-05-11 in production log
(`tgdb.bug.log`) when the user's binary had 107 locals at the stopped
frame.  The log shows the user repeatedly pressing Enter on the
console while tgdb was sending MI commands on the secondary channel.
The crashing sequence was:

```
MI->: b'1597-stack-list-variables --all-values'     (triggers call_function_by_hand)
GDB input: b'\r'                                     (console Enter, repeats "next")
MI<-: &"infcall.c:1594: internal-error: ..."         (assertion failure)
```

### GDB source analysis

The bug is in `gdb/infcall.c` (`call_function_by_hand_dummy`) and
`gdb/infcall.c` (`run_inferior_call`):

- Line 808: `current_ui->unregister_file_handler()` — only the
  current UI (MI), not the console UI.
- Line 813: `clear_proceed_status(0)` — clears old FSMs.
- Line 817: `set_thread_fsm(sm)` — installs new infcall FSM.
- Line 835: `proceed()` → line 853: `wait_sync_command_done()` →
  event loop via `gdb_do_one_event()`.
- The event loop (`gdbsupport/event-loop.cc:189`) processes I/O from
  **all** registered file descriptors (all UIs), round-robin.
- If console input arrives during this loop, it dispatches the
  console handler → step/next → `clear_proceed_status(1)` →
  `clear_proceed_status_thread()` at `infrun.c:3078` calls
  `release_thread_fsm()` on every thread → destroys `sm`.
- Back in `call_function_by_hand_dummy` at line 1594:
  `gdb_assert(call_thread->thread_fsm() == sm)` fails.

### tgdb workaround

From tgdb's side, two mitigations reduce exposure:

1. **Remove `-stack-list-variables --all-values`**: this command is the
   primary trigger because it internally invokes `call_function_by_hand`
   for complex C++ types.  Using `get_locals_b64()` (GDB-side Python)
   instead avoids the inferior call path.

2. **Cancel-and-replace for heavy MI commands**: only one in-flight
   heavy command at a time prevents MI queue pile-up.

Planned architectural fix: move all heavy data collection to the
existing pipe between tgdb and GDB (json → zlib → pipe), keeping the
MI channel for lightweight control commands only.

### Reproducer

Run [`../known_gdb_bug_reproduce/bug_3.py`](../known_gdb_bug_reproduce/bug_3.py).
Exit code 0 = bug reproduced (GDB asserted / exited), 1 = not reproduced,
2 = `gdb`/`g++` not in PATH, 3 = setup diverged.

The reproducer creates a binary with `std::string` locals, stops at a
breakpoint, then simultaneously:
- sends `-data-evaluate-expression "s1 + s2 + s3"` on the MI channel
  (triggers `call_function_by_hand` via `operator+`)
- spams Enter on the primary console (repeats `next`)

**Status**: the race window is extremely narrow with simple types.
The reproducer has not yet reliably triggered the crash.  In production,
the crash appears to require complex C++ types (smart pointers, futures,
lambdas) whose value-printing takes long enough to widen the timing
window.  The reproducer is provided as a starting point for further
investigation with more complex test binaries.
