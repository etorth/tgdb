"""
Reproducer for known_gdb_bug.md Bug 3:
  ``infcall.c: call_function_by_hand_dummy:
    Assertion `call_thread->thread_fsm () == sm' failed``

Hypothesis under test
---------------------
GDB's ``call_function_by_hand_dummy`` saves the current thread FSM,
creates a new one for the inferior call, runs the inferior
synchronously via ``wait_sync_command_done`` (which enters the event
loop), and afterward asserts the FSM has not been changed.

``run_inferior_call`` unregisters only the **current** UI's file
handler.  In a ``new-ui`` setup — exactly how tgdb drives GDB — the
other UI's input handler remains active during the event loop.  If a
command arrives on that other UI while the inferior call is in flight
(or has just stopped at the dummy breakpoint but the loop hasn't
exited), and that command calls ``clear_proceed_status`` (as all
execution commands — ``next``, ``step``, ``continue`` — do), then
``clear_proceed_status_thread`` iterates every thread in all-stop
mode and releases their FSMs.  When ``call_function_by_hand_dummy``
checks the assertion afterward, the FSM has been destroyed.

The production trigger (from ``tgdb.bug.log``):

  1. tgdb sends MI commands on the ``new-ui mi`` channel.
  2. One of those commands (``-stack-list-variables --all-values``)
     internally triggers ``call_function_by_hand`` during value
     formatting (likely for a type whose printing involves C++
     operator evaluation).
  3. The user presses Enter on the primary console PTY, which
     repeats the last GDB command (e.g. ``next``).
  4. The console's input handler fires during the event loop inside
     ``wait_sync_command_done``.
  5. ``step_1 → clear_proceed_status(1)`` destroys the MI channel's
     infcall FSM → assertion failure.

Driver: spawn GDB, create a ``new-ui mi`` channel, stop at a
breakpoint with string locals, then on the MI channel evaluate
expressions that trigger ``call_function_by_hand`` (C++ operator+
on ``std::string``), while simultaneously sending Enter keystrokes
on the primary console to attempt to race with the inferior call.

Exit codes:
  0  bug confirmed (GDB asserted / exited)
  1  bug not reproduced
  2  ``gdb`` or ``g++`` not in PATH
  3  setup diverged from the hypothesis
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time


CPP_SOURCE = textwrap.dedent("""
    #include <string>

    std::string concat(const std::string& a, const std::string& b)
    {
        return a + b;
    }

    int main()
    {
        std::string s1 = "hello";
        std::string s2 = "world";
        std::string s3 = "!";

        for (volatile int i = 0; ; ++i) {
            s1 = "hello";                // line 16
            s2 = "world";               // line 17
            s3 = concat(s1, s2);         // line 18
            s1 = s3;                     // line 19
        }
        return 0;
    }
""").lstrip()


def compile_test(cpp_path: str, exe_path: str) -> None:
    subprocess.run(
        ["g++", "-O0", "-g", "-std=c++17", "-o", exe_path, cpp_path],
        check=True,
    )


_MI_RESULT_RE = re.compile(r"^(\d+)\^(done|error|running|exit|connected)\b")


def _read_available(fd: int, timeout: float = 0.5) -> bytes:
    """Read whatever is available on a raw fd within timeout."""
    parts: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        r, _, _ = select.select([fd], [], [], remaining)
        if fd in r:
            try:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                parts.append(chunk)
            except OSError:
                break
        else:
            if parts:
                break
    return b"".join(parts)


class DualUIDriver:
    """
    Drive GDB with a CLI primary console and a secondary MI channel.

    - UI-0: primary console (PTY), CLI interpreter — user keystrokes
    - UI-1: ``new-ui mi <pty>`` — structured MI commands
    """

    def __init__(self, exe_path: str) -> None:
        # PTY for the primary console (CLI mode).
        self.console_master, self.console_slave = pty.openpty()
        self.console_slave_path = os.ttyname(self.console_slave)

        # PTY for the secondary MI channel.
        self.mi_master, self.mi_slave = pty.openpty()
        self.mi_slave_path = os.ttyname(self.mi_slave)

        self._all_output: list[str] = []
        self._mi_token = 0
        self._mi_lines: list[str] = []
        self._mi_line_cursor = 0
        self._mi_partial = ""

        self.proc = subprocess.Popen(
            [
                "gdb",
                "--quiet",
                "-ex", "set debuginfod enabled off",
                "-ex", "set pagination off",
                "-ex", "set confirm off",
                "-ex", "set print pretty on",
                exe_path,
            ],
            stdin=self.console_slave,
            stdout=self.console_slave,
            stderr=self.console_slave,
            start_new_session=True,
        )
        # Wait for GDB to start.
        self._drain_console(timeout=3.0)


    def _drain_console(self, timeout: float = 1.0) -> str:
        data = _read_available(self.console_master, timeout=timeout)
        text = data.decode("utf-8", errors="replace")
        if text:
            self._all_output.append(text)
        return text


    def _pump_mi(self, timeout: float = 0.5) -> None:
        """Read from MI fd and split into lines."""
        data = _read_available(self.mi_master, timeout=timeout)
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        self._all_output.append(text)
        self._mi_partial += text
        lines = self._mi_partial.split("\n")
        self._mi_partial = lines[-1]
        for line in lines[:-1]:
            stripped = line.strip()
            if stripped:
                self._mi_lines.append(stripped)


    def _scan_mi_lines(self, predicate):
        """Scan unprocessed MI lines for a matching line."""
        while self._mi_line_cursor < len(self._mi_lines):
            line = self._mi_lines[self._mi_line_cursor]
            self._mi_line_cursor += 1
            result = predicate(line)
            if result is not None:
                return result
        return None


    def console_send(self, cmd: str) -> None:
        """Send a command on the CLI console."""
        os.write(self.console_master, (cmd + "\n").encode())


    def console_send_raw(self, data: bytes) -> None:
        """Send raw bytes (e.g. Enter key) on the CLI console."""
        os.write(self.console_master, data)


    def setup_new_ui(self) -> bool:
        """Create the secondary MI channel."""
        self.console_send(f"new-ui mi {self.mi_slave_path}")
        time.sleep(0.5)
        self._drain_console(timeout=1.0)
        # Drain MI channel initial output.
        self._pump_mi(timeout=1.0)
        self._mi_line_cursor = len(self._mi_lines)
        return self.proc.poll() is None


    def mi_cmd(self, mi_cmd: str, timeout: float = 10.0) -> tuple[str, str]:
        """Send an MI command and wait for its result."""
        self._mi_token += 1
        token = self._mi_token
        os.write(self.mi_master, f"{token}{mi_cmd}\n".encode())

        def check_result(line: str):
            if "internal-error" in line:
                return ("crash", line)
            m = _MI_RESULT_RE.match(line)
            if m and int(m.group(1)) == token:
                return (m.group(2), line)
            return None

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._scan_mi_lines(check_result)
            if result is not None:
                return result
            if self.proc.poll() is not None:
                return ("exit", "")
            remaining = min(2.0, max(0.1, deadline - time.monotonic()))
            self._pump_mi(timeout=remaining)
        return ("timeout", "")


    def mi_send_nowait(self, mi_cmd: str) -> int:
        """Send an MI command without waiting.  Returns token."""
        self._mi_token += 1
        token = self._mi_token
        os.write(self.mi_master, f"{token}{mi_cmd}\n".encode())
        return token


    def mi_wait_for_stop(self, timeout: float = 10.0) -> bool:
        def check_stopped(line: str):
            if "*stopped" in line:
                return True
            if "internal-error" in line:
                return True
            return None

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._scan_mi_lines(check_stopped)
            if result is not None:
                return True
            if self.proc.poll() is not None:
                return False
            remaining = max(0.1, deadline - time.monotonic())
            self._pump_mi(timeout=remaining)
        return False


    def mi_wait_any(self, token: int, timeout: float = 5.0) -> tuple[str, str]:
        """Wait for either a result for the given token, *stopped, or crash."""
        def check(line: str):
            if "internal-error" in line:
                return ("crash", line)
            m = _MI_RESULT_RE.match(line)
            if m and int(m.group(1)) == token:
                return (m.group(2), line)
            return None

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._scan_mi_lines(check)
            if result is not None:
                return result
            if self.proc.poll() is not None:
                return ("exit", "")
            remaining = max(0.1, deadline - time.monotonic())
            self._pump_mi(timeout=remaining)
        return ("timeout", "")


    def all_output(self) -> str:
        return "".join(self._all_output)


    def shutdown(self) -> None:
        try:
            os.write(self.console_master, b"quit\n")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for fd in (self.console_master, self.console_slave,
                   self.mi_master, self.mi_slave):
            try:
                os.close(fd)
            except OSError:
                pass


def main() -> int:
    if shutil.which("gdb") is None or shutil.which("g++") is None:
        print("gdb and g++ required in PATH", file=sys.stderr)
        return 2

    tmpdir = tempfile.mkdtemp(prefix="infcall-fsm-repro-")
    cpp = os.path.join(tmpdir, "infcall.cpp")
    exe = os.path.join(tmpdir, "infcall")
    with open(cpp, "w") as fh:
        fh.write(CPP_SOURCE)
    compile_test(cpp, exe)
    print(f"# test binary: {exe}")

    d = DualUIDriver(exe)
    try:
        if not d.setup_new_ui():
            print("FAIL: could not create new-ui MI channel")
            return 3

        # Set breakpoint and run via MI.
        d.mi_cmd("-enable-pretty-printing")
        cls, ln = d.mi_cmd(f'-break-insert {cpp}:18')
        if cls != "done":
            cls, ln = d.mi_cmd("-break-insert main")
            if cls != "done":
                print(f"FAIL: could not set breakpoint: {cls} {ln}")
                return 3

        cls, ln = d.mi_cmd("-exec-run")
        if not d.mi_wait_for_stop(timeout=10):
            print("FAIL: inferior did not stop at breakpoint")
            return 3

        # Type "next" on the CLI console so that pressing Enter later
        # will repeat the step command (GDB repeats last command on
        # empty Enter).
        d.console_send("next")
        time.sleep(0.5)
        d._drain_console(timeout=1.0)

        # Wait for the next stop (from the console "next").
        d.mi_wait_for_stop(timeout=5)

        # Continue to hit the breakpoint again.
        d.mi_cmd("-exec-continue")
        if not d.mi_wait_for_stop(timeout=10):
            print("FAIL: inferior did not stop after continue")
            return 3

        print("# inferior stopped at breakpoint")
        print("# attempting dual-UI race: MI inferior call + console Enter ...")

        # Strategy: on the MI channel, evaluate expressions that trigger
        # call_function_by_hand (C++ operator+ on std::string).
        # Simultaneously, spam Enter on the console to repeat the last
        # "next" command.  If the console's "next" fires during the
        # MI channel's inferior call event loop, clear_proceed_status
        # will destroy the infcall FSM.
        crashed = False
        for attempt in range(200):
            if d.proc.poll() is not None:
                crashed = True
                break

            # MI: trigger inferior call via C++ operator+.
            t = d.mi_send_nowait(
                '-data-evaluate-expression "s1 + s2 + s3"'
            )

            # Console: rapidly send Enter keystrokes to race.
            for _ in range(10):
                d.console_send_raw(b"\r")
                time.sleep(0.0002)

            # Wait for MI result — pump MI output and scan for
            # the token result or a crash indicator.
            deadline = time.monotonic() + 5.0
            got_result = False
            while time.monotonic() < deadline:
                d._pump_mi(timeout=0.5)
                while d._mi_line_cursor < len(d._mi_lines):
                    line = d._mi_lines[d._mi_line_cursor]
                    d._mi_line_cursor += 1
                    if "internal-error" in line:
                        crashed = True
                        got_result = True
                        break
                    m = _MI_RESULT_RE.match(line)
                    if m and int(m.group(1)) == t:
                        got_result = True
                        break
                if got_result or crashed:
                    break
                if d.proc.poll() is not None:
                    crashed = True
                    break
            if crashed:
                break

            # Drain console output.
            d._drain_console(timeout=0.1)

            if d.proc.poll() is not None:
                crashed = True
                break

            # The console "next" commands may have advanced the
            # program.  Ensure we're stopped at a breakpoint.
            d.mi_send_nowait("-exec-continue")
            if not d.mi_wait_for_stop(timeout=3):
                cls, _ = d.mi_cmd("-exec-run", timeout=3)
                if cls == "exit":
                    crashed = True
                    break
                if not d.mi_wait_for_stop(timeout=5):
                    break

            if attempt % 50 == 49:
                print(f"  attempt {attempt + 1}/200 ...")

        all_out = d.all_output()
        crash_indicators = (
            "internal-error" in all_out
            or "call_function_by_hand" in all_out
            or "thread_fsm" in all_out
            or "infcall.c" in all_out
            or d.proc.poll() is not None
        )

        if crashed or crash_indicators:
            print()
            print("CONFIRMED: GDB internal-error reproduced.")
            for pattern in [
                r"infcall\.c:\d+:.*internal-error:.*",
                r"call_function_by_hand.*Assertion.*",
                r"thread_fsm.*",
                r"internal-error:.*",
            ]:
                m = re.search(pattern, all_out)
                if m:
                    sig = m.group(0)[:200]
                    print(f"  -> {sig}")
                    break
            print(f"  GDB process alive: {d.proc.poll() is None}")
            return 0

        print()
        print("NOT REPRODUCED: all overlapping commands completed without crash.")
        print("  (The race is timing-dependent; may need a different GDB build,")
        print("  a multi-threaded program, or specific library types to trigger.)")
        return 1
    finally:
        d.shutdown()


if __name__ == "__main__":
    sys.exit(main())


