python << EOF
from tgdb.workspace import PaneContainer

# Default layout should have source + gdb in a vertical split
container = app.query_one("#split-container", PaneContainer)
assert container is not None, "split-container not found"
assert len(container.items) == 2, f"expected 2 items in default layout, got {len(container.items)}"
assert container.orientation == "vertical", f"expected vertical, got {container.orientation}"
EOF

# Now test custom layout via API
python << EOF
await tgdb.screen.close_all_panes()

from tgdb.workspace import PaneContainer
container = app.query_one("#split-container", PaneContainer)
assert len(container.items) == 0, f"expected empty after close_all, got {len(container.items)}"

await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.HORIZONTAL)
container = app.query_one("#split-container", PaneContainer)
assert len(container.items) == 2, f"expected 2 after h-split, got {len(container.items)}"

await tgdb.screen.get_pane([0]).attach(tgdb.Pane.SOURCE)
await tgdb.screen.get_pane([1]).attach(tgdb.Pane.GDB)

src = app._get_source_view()
gdb_w = app._get_gdb_widget()
assert src is not None and src.parent is not None, "source pane not attached"
assert gdb_w is not None and gdb_w.parent is not None, "gdb pane not attached"
EOF
quit
