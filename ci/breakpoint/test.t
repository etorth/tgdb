python << EOF
import asyncio
await asyncio.sleep(0.5)

# Set breakpoint at main and run
app.gdb.send_input(b"break main\r")
await asyncio.sleep(0.3)
app.gdb.send_input(b"run\r")
await asyncio.sleep(1.5)

# Verify we stopped
src = app._get_source_view()
assert src is not None, "source view is None"
assert src._current_file is not None, "no source file loaded after breakpoint hit"
assert "fixture" in src._current_file, f"unexpected file: {src._current_file}"
assert src._exec_line is not None and src._exec_line > 0, "no executing line set"
EOF
quit
