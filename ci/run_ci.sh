#!/bin/bash
# Run all CI regression tests under ci/.
# Usage: ci/run_all.sh [test_name ...]
#   With no arguments, runs all tests.
#   With arguments, runs only the named tests.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CI_DIR="$REPO_ROOT/ci"
RUN_BASE="${TMPDIR:-/tmp}/tgdb-ci-$$"

mkdir -p "$RUN_BASE"

passed=0
failed=0
errors=""

if [ $# -gt 0 ]; then
    tests=("$@")
else
    tests=()
    for d in "$CI_DIR"/*/; do
        [ -f "$d/run.sh" ] && tests+=("$(basename "$d")")
    done
fi

for test_name in "${tests[@]}"; do
    test_dir="$CI_DIR/$test_name"
    if [ ! -f "$test_dir/run.sh" ]; then
        echo "  SKIP  $test_name (no run.sh)"
        continue
    fi

    run_dir="$RUN_BASE/$test_name"
    mkdir -p "$run_dir"

    echo -n "  RUN   $test_name ... "
    if bash "$test_dir/run.sh" "$run_dir"; then
        echo "PASS"
        passed=$((passed + 1))
    else
        echo "FAIL (see $run_dir/run.log)"
        failed=$((failed + 1))
        errors="$errors  FAIL: $test_name\n"
    fi
done

echo ""
echo "Results: $passed passed, $failed failed (logs: $RUN_BASE)"
if [ $failed -gt 0 ]; then
    echo -e "$errors"
    exit 1
fi
