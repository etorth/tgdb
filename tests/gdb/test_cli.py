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
        "assert app.cfg.tabstop == 4\n"
        "container = app.query_one('#global-container')\n"
        "assert (container.styles.width.value, container.styles.height.value) == (90, 33)\n"
        "await tgdb.debugger.execute('break add')\n"
        "await tgdb.debugger.execute('run')\n"
        "path, line = tgdb.debugger.source_position()\n"
        "assert path.endswith('basic.c') and line > 0, (path, line)\n"
        "assert await tgdb.debugger.evaluate('a + b') == '30'\n"
        "EOF\n"
        "quit\n"
    ))
    result = tgdb("--width", "90", "--height", "33", "--batch", script, "-r", "NONE",
                  *GDB_ISOLATION_ARGS, str(build("basic.c")))
    assert result.returncode == 0, result.stderr


def test_failing_batch_script_exits_1_with_reason(build, tmp_path):
    script = write(tmp_path / "bad.t", "python << EOF\nassert False, 'boom-marker'\nEOF\n")
    result = tgdb("--batch", script, "-r", "NONE", *GDB_ISOLATION_ARGS, str(build("basic.c")))
    assert result.returncode == 1
    assert "boom-marker" in result.stderr


def test_bad_rc_file_exits_1(build, tmp_path):
    """Regression: the gdb-exit callback overwrote the rc failure's exit code."""
    rc = write(tmp_path / "rc", "set bogus\n")
    script = write(tmp_path / "never.t", "quit\n")
    result = tgdb("--batch", script, "-r", rc, *GDB_ISOLATION_ARGS, str(build("basic.c")))
    assert result.returncode == 1


def test_args_passes_program_arguments(build, tmp_path):
    """``--args prog a b`` gives the program its arguments, like gdb/cgdb."""
    script = write(tmp_path / "args.t", (
        "python << EOF\n"
        "await tgdb.debugger.execute('break main')\n"
        "await tgdb.debugger.execute('run')\n"
        "assert await tgdb.debugger.evaluate('argc') == '3'\n"
        "assert '\"two words\"' in await tgdb.debugger.evaluate('argv[2]')\n"
        "EOF\n"
    ))
    result = tgdb("--batch", script, "-r", "NONE", *GDB_ISOLATION_ARGS,
                  "--args", str(build("args.c")), "one", "two words")
    assert result.returncode == 0, result.stderr
