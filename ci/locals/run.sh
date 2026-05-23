#!/bin/bash
# CI test: verify local variables are populated after stopping.
set -euo pipefail

RUN_DIR="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

gcc -g "$SCRIPT_DIR/fixture.c" -o "$RUN_DIR/a.out"

python -m tgdb \
    --headless \
    --batch "$SCRIPT_DIR/test.t" \
    --log "$RUN_DIR/tgdb.log" \
    -r NONE \
    --args "$RUN_DIR/a.out" \
    > "$RUN_DIR/run.log" 2>&1
