"""CI test runner for map."""

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
    ]
    with (run_dir / "run.log").open("w", encoding="utf-8") as handle:
        result = subprocess.run(
            command,
            cwd=repo_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
