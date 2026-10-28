"""End-to-end ``python -m tgdb --headless`` runs: rc files, --batch, exit codes."""

import subprocess
import sys
from pathlib import Path

from .harness import GDB_ISOLATION_ARGS

REPO = Path(__file__).resolve().parents[2]


def tgdb(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tgdb", "--headless", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_batch_script_sees_ui_updates(build, tmp_path):
    """Regression: the batch script ran inside on_mount and blocked every UI update."""
    script = write(tmp_path / "ok.t", (
        "set tabstop=4\n"
        "python << EOF\n"
        "import asyncio\n"
        "assert app.cfg.tabstop == 4\n"
        "assert app._target_width == 90 and app._target_height == 33\n"
        "app.gdb.send_input(b'break add\\r')\n"
        "await asyncio.sleep(0.5)\n"
        "app.gdb.send_input(b'run\\r')\n"
        "for _ in range(100):\n"
        "    await asyncio.sleep(0.1)\n"
        "    if app._get_source_view().exe_line > 0:\n"
        "        break\n"
        "assert app._get_source_view().exe_line > 0, 'source pane never saw the stop'\n"
        "EOF\n"
        "quit\n"
    ))
    result = tgdb("--width", "90", "--height", "33", "--batch", script, "-r", "NONE",
                  "--args", *GDB_ISOLATION_ARGS, str(build("basic.c")))
    assert result.returncode == 0, result.stderr


def test_failing_batch_script_exits_1_with_reason(build, tmp_path):
    script = write(tmp_path / "bad.t", "python << EOF\nassert False, 'boom-marker'\nEOF\n")
    result = tgdb("--batch", script, "-r", "NONE", "--args", *GDB_ISOLATION_ARGS, str(build("basic.c")))
    assert result.returncode == 1
    assert "boom-marker" in result.stderr


def test_bad_rc_file_exits_1(build, tmp_path):
    """Regression: the gdb-exit callback overwrote the rc failure's exit code."""
    rc = write(tmp_path / "rc", "set bogus\n")
    script = write(tmp_path / "never.t", "quit\n")
    result = tgdb("--batch", script, "-r", rc, "--args", *GDB_ISOLATION_ARGS, str(build("basic.c")))
    assert result.returncode == 1
