# Known GDB Bugs

Catalogue of GDB defects that tgdb has hit in practice, with self-contained
reproducers.  Each entry is independent — add new ones by appending a new
`## Bug N: ...` section.

For every bug we record:

- **Symptom** — what the user sees.
- **Trigger** — the exact MI sequence that provokes it.
- **Crash signature** — the assert / error string GDB emits.
- **Reproducer** — a standalone script we can run to verify the bug still
  exists on a given GDB build.  Reproducers should depend only on `gdb`,
  `g++`, and the Python standard library so they survive across machines.
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

Save the following script and run with `python3` while
`gdb` and `g++` are on `$PATH`.  Exit codes:

- `0` — bug confirmed (GDB asserted / exited).
- `1` — bug not reproduced (GDB completed the final command cleanly).
- `2` — `gdb` or `g++` not in PATH.
- `3` — printer behavior diverged from the hypothesis (e.g. no
  `[contained value]` child for the string-holding variant), so we
  could not even set up the trigger.

```python
"""
Reproducer for GDB std::variant alternative-switch crash.

Hypothesis under test:
  When -var-create is used on a std::variant and -var-list-children
  registers a child named ``[contained value]`` while the variant
  holds a string-like alternative, then the variant is reassigned to
  a vector<int> alternative and -var-update reports the new array-
  indexed shape, a follow-up -var-list-children on the same varobj
  crashes GDB at gdb/varobj.c:install_new_value.

Driver: spawn ``gdb --interpreter=mi``, drive it with tokenized MI
commands.  After each command, read until the matching tokenized
response (^done/^error/^running/^exit) arrives.  Track *stopped
events separately so we can wait for inferior stops after exec
commands.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time


CPP_SOURCE = textwrap.dedent("""
    #include <string>
    #include <vector>
    #include <variant>

    int main()
    {
        std::variant<std::string, std::vector<int>> v {};
        v = std::string{"string"};
        v = std::vector<int>{1,2,3};
        return 0;
    }
""").lstrip()


def compile_test(cpp_path: str, exe_path: str) -> None:
    subprocess.run(
        ["g++", "-O0", "-g", "-std=c++17", "-o", exe_path, cpp_path],
        check=True,
    )


_RESULT_RE = re.compile(r"^(\d+)\^(done|error|running|exit|connected)\b")


class MISession:
    def __init__(self, exe_path: str) -> None:
        self.proc = subprocess.Popen(
            [
                "gdb",
                "--interpreter=mi",
                "--quiet",
                "-ex", "set debuginfod enabled off",
                "-ex", "set print pretty on",
                exe_path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._token = 0
        self._buf: list[str] = []
        self._stopped_pending = False
        self._drain_initial()

    def _readline(self, timeout: float = 5.0) -> str | None:
        # Synchronous readline with a wall-clock deadline driven by
        # poll().  Returns None on EOF / timeout.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                # Drain remaining buffered output then return.
                line = self.proc.stdout.readline()
                return line if line else None
            line = self.proc.stdout.readline()
            if line:
                return line
        return None

    def _drain_initial(self) -> None:
        # Read until the first (gdb) prompt.
        while True:
            line = self._readline()
            if line is None:
                return
            line = line.rstrip("\n")
            self._buf.append(line)
            if line.startswith("(gdb)"):
                return

    def cmd(self, mi_cmd: str, timeout: float = 5.0) -> tuple[str, str]:
        """Send an MI command; return (result_class, full_response_line)."""
        self._token += 1
        token = self._token
        line = f"{token}{mi_cmd}\n"
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return ("exit", "")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ln = self._readline(timeout=max(0.1, deadline - time.monotonic()))
            if ln is None:
                if self.proc.poll() is not None:
                    return ("exit", "")
                continue
            ln = ln.rstrip("\n")
            self._buf.append(ln)
            if ln.startswith("*stopped"):
                self._stopped_pending = True
            m = _RESULT_RE.match(ln)
            if m and int(m.group(1)) == token:
                return (m.group(2), ln)
        return ("timeout", "")

    def wait_for_stop(self, timeout: float = 5.0) -> bool:
        """Block until a *stopped event has arrived since the last call."""
        if self._stopped_pending:
            self._stopped_pending = False
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ln = self._readline(timeout=max(0.1, deadline - time.monotonic()))
            if ln is None:
                if self.proc.poll() is not None:
                    return False
                continue
            ln = ln.rstrip("\n")
            self._buf.append(ln)
            if ln.startswith("*stopped"):
                return True
        return False

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def joined_output(self) -> str:
        return "\n".join(self._buf)

    def shutdown(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write("9999-gdb-exit\n")
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    if shutil.which("gdb") is None or shutil.which("g++") is None:
        print("gdb and g++ required in PATH", file=sys.stderr)
        return 2

    tmpdir = tempfile.mkdtemp(prefix="variant-repro-")
    cpp = os.path.join(tmpdir, "v.cpp")
    exe = os.path.join(tmpdir, "v")
    with open(cpp, "w") as fh:
        fh.write(CPP_SOURCE)
    compile_test(cpp, exe)
    print(f"# test binary: {exe}")
    print(f"# CPP_SOURCE: {cpp}")

    s = MISession(exe)
    try:
        s.cmd("-enable-pretty-printing")
        s.cmd("-break-insert main")
        s.cmd("-exec-run")
        s.wait_for_stop()
        s.cmd("-exec-next")          # step past entry brace
        s.wait_for_stop()
        s.cmd("-exec-next")          # past the variant declaration -> at line 8
        s.wait_for_stop()
        s.cmd("-exec-next")          # past `v = std::string{"string"}` -> at line 9, v holds string
        s.wait_for_stop()

        cls, ln = s.cmd('-var-create vv * "v"')
        print(f"[1] var-create  ({cls}): {ln}")
        if cls != "done":
            print("FAIL: var-create did not return ^done")
            return 3

        cls, ln = s.cmd("-var-list-children --all-values vv")
        print(f"[2] var-list-children (string state) ({cls}): {ln}")
        if cls != "done" or "[contained value]" not in ln:
            print("FAIL: expected '[contained value]' child for string-holding variant")
            return 3

        s.cmd("-exec-next")          # past `v = std::vector<int>{1,2,3}` -> alternative flips
        s.wait_for_stop()

        cls, ln = s.cmd("-var-update --all-values vv")
        print(f"[3] var-update (after vector assignment) ({cls}): {ln}")
        if cls != "done" or "new_num_children" not in ln:
            print("FAIL: expected new_num_children in var-update reply")
            return 3

        # The crash trigger.
        cls, ln = s.cmd("-var-list-children --all-values vv 0 10", timeout=4.0)
        print(f"[4] var-list-children (post-transition) ({cls}): {ln}")

        joined = s.joined_output()
        crashed = (
            "internal-error" in joined
            or "install_new_value" in joined
            or "var->value->lazy" in joined
            or "!print_value.empty" in joined
            or not s.is_alive()
        )
        if crashed:
            m = re.search(r"varobj\.c:\d+:\s*internal-error:\s*[^\\]+", joined)
            print()
            print("CONFIRMED: GDB internal-error reproduced.")
            if m:
                print(f"  -> {m.group(0)}")
            print(f"  GDB process alive: {s.is_alive()}")
            return 0

        print()
        print("NOT REPRODUCED: var-list-children completed without crash.")
        return 1
    finally:
        s.shutdown()


if __name__ == "__main__":
    sys.exit(main())
```

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
Bug 1.  Reproduced 2026-05-09 against a real workload (the user's
in-house server source).  We do not yet have a reduced standalone
reproducer because the trigger depends on the exact class layout
of the parent type — it has not fired on synthetic test inputs we
have tried.  When a reduced reproducer is available, append it to
this entry.

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

We do not have a runnable reproducer yet.  The crash has only
been observed against a real workload; minimal test programs that
exercise `std::vector<std::vector<long>>` inside a class with
public access have not reproduced it on the same GDB build.  The
class layout in the failing case likely has additional structure
(multiple inheritance, anonymous unions, base-class fields,
template depth) that confuses GDB's c-varobj access tracking.

When a reduced reproducer is available, replace this section with
a self-contained script following the format of Bug 1.
