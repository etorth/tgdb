"""
Reproducer for GDB ``c-varobj.c:cplus_describe_child: Assertion 'access' failed``.

Mirrors the failing MI sequence from the user's tgdb session at
2026-05-09 07:26:23 (see /home/anhong/win_desktop/tgdb.bug.log lines
139194-139213, also Bug 2 in docs/known_gdb_bug.md):

    2161-var-update --all-values "var36.public.index.[0]"   ^done
    2162-var-update --all-values "var36.public.index.[1]"   ^done value=""
    2163-var-update --all-values "var36.public.index.[2]"
        c-varobj.c:860: internal-error: cplus_describe_child:
            Assertion `access' failed.

This script:

1. Synthesises a class with public access whose member ``index`` is a
   ``std::vector<std::vector<long>>`` and a couple of plain int
   members in front of it (matching the field family observed in the
   log: ``mask``, ``connector``, ``offset``, then ``index``).
2. Compiles it with debug info.
3. Spawns ``gdb --interpreter=mi``, drives it through the same flow
   tgdb does in the locals pane: ``-var-create`` on the root,
   ``-var-list-children`` to register synthetic children
   (``.public.<field>``), recurse into ``index`` so its
   ``[0],[1],[2]`` children become tracked varobjs, then
   ``-var-update`` each one individually.
4. Checks whether the third ``-var-update`` (or any subsequent one)
   triggers the cplus_describe_child assertion.

Exit codes:

  0  bug confirmed (GDB asserted / exited mid-update)
  1  bug not reproduced (all updates completed cleanly)
  2  ``gdb`` or ``g++`` not in PATH
  3  test setup failed before the crash trigger could be exercised
     (e.g. children of expected names did not appear)

Standalone — depends only on ``gdb``, ``g++``, and the Python stdlib.
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


_BUG_ID = "Bug 2"
_BUG_TITLE = "c-varobj.c:cplus_describe_child assertion on -var-update of vector<vector<long>> children"

_EXIT_LABELS = {
    0: "CONFIRMED",
    1: "NOT REPRODUCED",
    2: "TOOLS MISSING",
    3: "SETUP FAILED",
}


def _verdict(code: int, detail: str = "") -> int:
    label = _EXIT_LABELS.get(code, "UNKNOWN")
    print()
    print("=" * 70)
    print(f"  {_BUG_ID}: {_BUG_TITLE}")
    print(f"  Result : {label} (exit code {code})")
    if detail:
        for line in detail.splitlines():
            print(f"  {line}")
    print("=" * 70)
    return code


CPP_SOURCE = textwrap.dedent("""
    #include <vector>

    // Mirrors the field family from the user's failing case:
    //   var36.public.mask
    //   var36.public.connector
    //   var36.public.offset
    //   var36.public.index   (std::vector<std::vector<long>>)
    // The inner element type is std::vector<long> so each
    // var36.public.index.[N] is itself a non-trivial varobj whose
    // describe_child path GDB has to walk.
    struct Container
    {
        int mask;
        int connector;
        int offset;
        std::vector<std::vector<long>> index;
    };

    int main()
    {
        Container c {};
        c.mask = 1;
        c.connector = 2;
        c.offset = 3;
        c.index.push_back({10, 20, 30});
        c.index.push_back({});             // empty inner vector
        c.index.push_back({40, 50});
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
    """Tokenised, async-event-aware driver for ``gdb --interpreter=mi``."""

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
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                line = self.proc.stdout.readline()
                return line if line else None
            line = self.proc.stdout.readline()
            if line:
                return line
        return None

    def _drain_initial(self) -> None:
        while True:
            line = self._readline()
            if line is None:
                return
            line = line.rstrip("\n")
            self._buf.append(line)
            if line.startswith("(gdb)"):
                return

    def cmd(self, mi_cmd: str, timeout: float = 5.0) -> tuple[str, str]:
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


def crashed(joined: str, alive: bool) -> bool:
    if not alive:
        return True
    markers = [
        "internal-error",
        "cplus_describe_child",
        "Assertion `access'",
        "install_new_value",
    ]
    return any(m in joined for m in markers)


def main() -> int:
    if shutil.which("gdb") is None or shutil.which("g++") is None:
        return _verdict(2, "gdb and g++ required in PATH")

    tmpdir = tempfile.mkdtemp(prefix="cplus-repro-")
    cpp = os.path.join(tmpdir, "v.cpp")
    exe = os.path.join(tmpdir, "v")
    with open(cpp, "w") as fh:
        fh.write(CPP_SOURCE)
    compile_test(cpp, exe)
    print(f"# test binary: {exe}")
    print(f"# test source: {cpp}")
    print()

    s = MISession(exe)
    try:
        s.cmd("-enable-pretty-printing")
        # Break at end of main so all assignments have happened.
        s.cmd("-break-insert -t 30")  # placeholder, will redo by line
        # Use a function-name break at main and step through assignments
        # to the close brace, where ``c`` is fully populated.
        s.cmd("-break-insert main")
        s.cmd("-exec-run")
        if not s.wait_for_stop(timeout=10):
            return _verdict(3, "did not stop at main breakpoint")
        # Step through every assignment + the three push_back calls.
        # ``-exec-finish`` would leave main; ``-exec-next`` repeatedly
        # works in any libstdc++ version.  Iterate generously and
        # bail out when we are still in main but past every push_back.
        for _ in range(30):
            cls, ln = s.cmd("-exec-next")
            if cls == "exit":
                break
            if not s.wait_for_stop(timeout=5):
                break
            # Stop once ``c.index.size() == 3``.
            cls, ln = s.cmd('-data-evaluate-expression "c.index.size()"')
            if cls == "done" and 'value="3"' in ln:
                break

        # ----- now reproduce tgdb's locals-pane MI sequence -----

        cls, ln = s.cmd('-var-create vv * "c"')
        print(f"[1] var-create vv:    ({cls}) {ln}")
        if cls != "done":
            return _verdict(3, "var-create on c failed")

        # Walk the children: vv -> vv.public -> vv.public.<field>
        cls, ln = s.cmd("-var-list-children --all-values vv")
        print(f"[2] children of vv:   ({cls}) {ln}")
        if cls != "done":
            return _verdict(3, "var-list-children on vv failed")

        # Find the access-specifier synthetic child name (vv.public).
        m = re.search(r'name="(vv\.public)"', ln)
        if not m:
            # Some GDB builds inline class members directly without a
            # public/private synthetic child.  Skip the synthetic step
            # in that case and try walking vv directly.
            access_name = "vv"
        else:
            access_name = m.group(1)
            cls, ln = s.cmd(f'-var-list-children --all-values "{access_name}"')
            print(f"[3] children of {access_name}: ({cls}) {ln}")
            if cls != "done":
                return _verdict(3, f"var-list-children on {access_name} failed")

        # Find the ``index`` child name and walk it.
        m = re.search(r'name="([^"]*\.index)"', ln)
        if not m:
            return _verdict(3, "did not find an .index child under access node")
        index_name = m.group(1)
        cls, ln = s.cmd(f'-var-list-children --all-values "{index_name}"')
        print(f"[4] children of {index_name}: ({cls}) {ln}")
        if cls != "done":
            return _verdict(3, f"var-list-children on {index_name} failed")

        # Children should be ``<index_name>.[0]``, ``[1]``, ``[2]``.
        child_names = re.findall(rf'name="({re.escape(index_name)}\.\[\d+\])"', ln)
        print(f"    -> tracked element children: {child_names}")
        if len(child_names) < 3:
            return _verdict(3, "expected at least 3 element children of index")

        # Now exercise the exact crash trigger: -var-update on each
        # element child, sequentially, in the same order tgdb does.
        for i, child in enumerate(child_names):
            cls, ln = s.cmd(f'-var-update --all-values "{child}"', timeout=4.0)
            alive = s.is_alive()
            print(f"[5.{i}] var-update {child}:  ({cls}) alive={alive}")
            if cls != "done" or not alive:
                joined = s.joined_output()
                if crashed(joined, alive):
                    m = re.search(
                        r"c-varobj\.c:\d+:\s*internal-error:\s*[^\\]+",
                        joined,
                    )
                    detail = f"GDB process alive: {alive}"
                    if m:
                        detail = f"{m.group(0)}\n{detail}"
                    return _verdict(0, detail)
                return _verdict(3, "var-update returned non-done without matching the assertion signature")

        return _verdict(
            1,
            "all element var-updates completed cleanly\n"
            "This is the expected outcome on GDB builds whose\n"
            "c-varobj.c::cplus_describe_child handles vector-of-vector\n"
            "member layouts without asserting.  See docs/known_gdb_bug.md Bug 2.",
        )
    finally:
        s.shutdown()


if __name__ == "__main__":
    sys.exit(main())
