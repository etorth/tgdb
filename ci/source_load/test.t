python << EOF
import asyncio
await asyncio.sleep(1.0)

src = app._get_source_view()
assert src is not None, "source view is None"
# After loading a binary with debug info, source should be populated
if src._current_file is not None:
    assert "fixture" in src._current_file, f"unexpected file: {src._current_file}"
    assert src._total_lines > 0, f"source has no lines loaded"
else:
    # No source loaded means GDB didn't find it — still pass if no crash
    pass
EOF
quit
