"""CI test runner for locals."""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run.py <run-dir>", file=sys.stderr)
        return 2

    run_dir = Path(sys.argv[1])
    run_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[1]
    binary_path = run_dir / "a.out"

    with (run_dir / "run.log").open("w", encoding="utf-8") as handle:
        compile_result = subprocess.run(
            ["gcc", "-g", str(script_dir / "fixture.c"), "-o", str(binary_path)],
            cwd=repo_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        if compile_result.returncode != 0:
            return compile_result.returncode

        command = [
            sys.executable,
            "-m",
            "tgdb",
            "--headless",
            "--batch",
            str(script_dir / "test.t"),
            "--log",
            str(run_dir / "tgdb.log"),
            "-r",
            "NONE",
            "--args",
            str(binary_path),
        ]
        result = subprocess.run(
            command,
            cwd=repo_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
