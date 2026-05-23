"""Run all CI regression tests under ``ci/``."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def _discover_tests(ci_dir: Path) -> list[str]:
    tests: list[str] = []
    for entry in sorted(ci_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "run.py").is_file():
            tests.append(entry.name)
    return tests


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ci/run_ci.py",
        description="Run tgdb CI regression tests.",
    )
    parser.add_argument(
        "tests",
        nargs="*",
        help="Optional list of test directories under ci/ to run",
    )
    args = parser.parse_args()

    ci_dir = Path(__file__).resolve().parent
    repo_root = ci_dir.parent
    run_base = Path(tempfile.mkdtemp(prefix="tgdb-ci-"))

    tests = args.tests or _discover_tests(ci_dir)
    passed = 0
    failed_tests: list[str] = []

    for test_name in tests:
        test_dir = ci_dir / test_name
        run_py = test_dir / "run.py"
        if not run_py.is_file():
            print(f"  SKIP  {test_name} (no run.py)")
            continue

        run_dir = run_base / test_name
        run_dir.mkdir(parents=True, exist_ok=True)

        print(f"  RUN   {test_name} ... ", end="", flush=True)
        result = subprocess.run(
            [sys.executable, str(run_py), str(run_dir)],
            cwd=repo_root,
        )
        if result.returncode == 0:
            print("PASS")
            passed += 1
            continue

        print(f"FAIL (see {run_dir / 'run.log'})")
        failed_tests.append(test_name)

    print()
    print(f"Results: {passed} passed, {len(failed_tests)} failed (logs: {run_base})")
    if failed_tests:
        for test_name in failed_tests:
            print(f"  FAIL: {test_name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
