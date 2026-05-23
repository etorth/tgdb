#!/bin/bash
# CI test: load a source file and verify source pane state.
set -euo pipefail

RUN_DIR="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

gcc -g "$CI_DIR/fixture.c" -o "$RUN_DIR/a.out"

python -m tgdb \
    --headless \
    --batch "$SCRIPT_DIR/test.t" \
    --log "$RUN_DIR/tgdb.log" \
    -r NONE \
    --args "$RUN_DIR/a.out" \
    > "$RUN_DIR/run.log" 2>&1
