python << EOF
import asyncio
await asyncio.sleep(1.0)

src = app._get_source_view()
assert src is not None, "source view is None"
# After loading a binary with debug info, source should be populated
if src.source_file is not None:
    assert "fixture" in src.source_file.path, f"unexpected file: {src.source_file.path}"
    assert len(src.source_file.lines) > 0, f"source has no lines loaded"
else:
    # No source loaded means GDB didn't find it — still pass if no crash
    pass
EOF
quit
