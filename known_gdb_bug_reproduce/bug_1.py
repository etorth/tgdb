"""
Reproducer for known_gdb_bug.md Bug 1:
  ``varobj.c:install_new_value`` assertion on
  ``-var-list-children`` after a ``std::variant`` alternative switch.

Hypothesis under test:
  When ``-var-create`` is used on a ``std::variant`` and
  ``-var-list-children`` registers a child named
  ``[contained value]`` while the variant holds a string-like
  alternative, then the variant is reassigned to a ``vector<int>``
  alternative and ``-var-update`` reports the new array-indexed
  shape, a follow-up ``-var-list-children`` on the same varobj
  crashes GDB at ``gdb/varobj.c:install_new_value``.

Driver: spawn ``gdb --interpreter=mi``, drive it with tokenised MI
commands.  After each command, read until the matching tokenised
response (``^done`` / ``^error`` / ``^running`` / ``^exit``)
arrives.  Track ``*stopped`` events separately so we can wait for
inferior stops after exec commands.

Exit codes:
  0  bug confirmed (GDB asserted / exited)
  1  bug not reproduced (post-transition var-list-children completed)
  2  ``gdb`` or ``g++`` not in PATH
  3  setup diverged from the hypothesis (missing ``[contained value]``,
     no ``new_num_children`` in var-update reply, etc.)
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


_BUG_ID = "Bug 1"
_BUG_TITLE = "varobj.c:install_new_value assertion on -var-list-children after std::variant switch"

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


def main() -> int:
    if shutil.which("gdb") is None or shutil.which("g++") is None:
        return _verdict(2, "gdb and g++ required in PATH")

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
        s.cmd("-exec-next")          # past variant declaration -> at line 8
        s.wait_for_stop()
        s.cmd("-exec-next")          # past `v = std::string{"string"}` -> at line 9
        s.wait_for_stop()

        cls, ln = s.cmd('-var-create vv * "v"')
        print(f"[1] var-create  ({cls}): {ln}")
        if cls != "done":
            return _verdict(3, "var-create did not return ^done")

        cls, ln = s.cmd("-var-list-children --all-values vv")
        print(f"[2] var-list-children (string state) ({cls}): {ln}")
        if cls != "done" or "[contained value]" not in ln:
            return _verdict(3, "expected '[contained value]' child for string-holding variant")

        s.cmd("-exec-next")          # past `v = std::vector<int>{1,2,3}` -> alternative flips
        s.wait_for_stop()

        cls, ln = s.cmd("-var-update --all-values vv")
        print(f"[3] var-update (after vector assignment) ({cls}): {ln}")
        if cls != "done" or "new_num_children" not in ln:
            return _verdict(3, "expected new_num_children in var-update reply")

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
            detail = f"GDB process alive: {s.is_alive()}"
            if m:
                detail = f"{m.group(0)}\n{detail}"
            return _verdict(0, detail)

        return _verdict(1, "var-list-children completed without crash")
    finally:
        s.shutdown()


if __name__ == "__main__":
    sys.exit(main())
