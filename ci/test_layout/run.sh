#!/bin/bash
# CI test: verify workspace layout commands (split, attach, close).
set -euo pipefail

RUN_DIR="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

python -m tgdb \
    --headless \
    --batch "$SCRIPT_DIR/test.t" \
    --log "$RUN_DIR/tgdb.log" \
    -r NONE \
    > "$RUN_DIR/run.log" 2>&1
