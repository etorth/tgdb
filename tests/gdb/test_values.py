"""Value formatting through the GDB-side collector, and the disasm pane."""

from tgdb.disasm_pane import DisasmPane
from tgdb.gdb_controller.types import normalize_addr

from .harness import run_session


def test_collected_values(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("values.c", "MARK_PRINT")
        collected = s.collected_locals()

        # Regression: an escaped backslash before digits raised in the
        # GDB-side decoder, reporting the local as <optimized out>.
        path = collected["path"]
        assert path.value == '"C:\\\\2024\\\\x"'
        assert path.addr.startswith("0x")

        assert "café 😀" in collected["smile"].value
        assert s.locals_value("n") == "42"

    run_session(build("values.c"), body)


def test_disasm_marks_pc(build):
    """Regression: socket-path PCs are unpadded, MI addresses are padded."""

    async def body(s):
        await s.run_to("values.c", "MARK_PRINT")
        await s.add_pane("disasm")
        panes = list(s.app.query(DisasmPane))
        assert len(panes) == 1
        content = panes[0]._content
        marked = [line.addr for line in content._lines if line.is_current]
        assert len(marked) == 1
        pc = normalize_addr(await s.gdb_eval("$pc"))
        assert int(marked[0], 16) == int(pc, 16)

    run_session(build("values.c"), body)
