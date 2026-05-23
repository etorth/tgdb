python << EOF
import asyncio
await asyncio.sleep(0.5)

# Set breakpoint at main, run, then step a few times to get locals
app.gdb.send_input(b"break main\r")
await asyncio.sleep(0.3)
app.gdb.send_input(b"run\r")
await asyncio.sleep(1.5)

# Step into function body to get local variables
app.gdb.send_input(b"next\r")
await asyncio.sleep(0.5)
app.gdb.send_input(b"next\r")
await asyncio.sleep(0.5)

# Request locals
await app.gdb.request_current_frame_locals(report_error=False)
await asyncio.sleep(0.5)

# Verify locals were populated (may be empty if at very start of main)
assert app._current_locals is not None, "_current_locals is None"
# At minimum, the request didn't crash — that's the key regression check
EOF
quit
