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
assert src.source_file is not None, "no source file loaded after breakpoint hit"
assert "fixture" in src.source_file.path, f"unexpected file: {src.source_file.path}"
assert src.exe_line > 0, "no executing line set"
EOF
quit
