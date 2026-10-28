"""Drive a real ``TGDBApp`` + GDB in-process for integration tests.

``run_session(binary, body)`` starts tgdb headless under Textual's test
driver, waits until it is idle, then awaits ``body(session)``.  ``Session``
is a thin layer over the public scripting API (``tgdb.screen`` and
``tgdb.debugger``), so tests exercise the same surface that ``:python``
and ``--batch`` scripts use.  The few white-box checks that need tgdb
internals are marked as such below.
"""

import asyncio
from pathlib import Path

import tgdb
from tgdb.app import TGDBApp
from tgdb.gdb_controller.types import LocalVariable

SESSION_TIMEOUT = 120.0
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Ignore ~/.gdbinit, and never stop at the interactive debuginfod prompt.
GDB_ISOLATION_ARGS = ["-nh", "-iex", "set debuginfod enabled off"]


class Session:
    def __init__(self, app: TGDBApp) -> None:
        self.app = app
        self.dbg = tgdb.debugger
        self.source_dir = FIXTURES


    def line_of(self, filename: str, marker: str) -> int:
        """1-based line of the unique line in *filename* containing *marker*."""
        lines = (self.source_dir / filename).read_text(encoding="utf-8").splitlines()
        hits = [n for n, line in enumerate(lines, start=1) if marker in line]
        assert len(hits) == 1, f"marker {marker!r} found on lines {hits} of {filename}"
        return hits[0]


    async def layout(self, *panes: str) -> None:
        """Replace the workspace with a vertical stack of *panes*."""
        await tgdb.screen.close_all_panes()
        for _ in range(len(panes) - 1):
            await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.VERTICAL)
        for index, pane in enumerate(panes):
            await tgdb.screen.get_pane([index]).attach(getattr(tgdb.Pane, pane.upper()))
        await self.dbg.wait_idle()


    async def append_pane(self, pane: str) -> None:
        """Add *pane* as a new cell at the end of the root container."""
        await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.VERTICAL)
        cells = len(self.app.query_one("#split-container").items)
        await tgdb.screen.get_pane([cells - 1]).attach(getattr(tgdb.Pane, pane.upper()))
        await self.dbg.wait_idle()


    async def cli(self, command: str) -> None:
        await self.dbg.execute(command)


    async def run_to(self, filename: str, marker: str) -> None:
        """Break on the line containing *marker* and ``run`` to it."""
        await self.cli(f"break {filename}:{self.line_of(filename, marker)}")
        await self.cli("run")


    async def gdb_value(self, expr: str) -> str:
        return await self.dbg.evaluate(expr)


    def locals_value(self, name: str) -> str | None:
        return dict(self.dbg.locals()).get(name)


    def collected_locals(self) -> dict[str, LocalVariable]:
        return {var.name: var for var in self.dbg.local_variables()}


    # White-box: placeholder bookkeeping is a locals-pane internal.
    def locals_placeholders(self) -> list[str]:
        return [name for name, _addr in self.app._locals_pane._uninitialized_nodes]


    # White-box: rows the disasm pane marks as the PC.
    def disasm_pc_rows(self) -> list[str]:
        from tgdb.disasm_pane import DisasmPane

        rows: list[str] = []
        for pane in self.app.query(DisasmPane):
            for line in pane._content._lines:
                if line.is_current:
                    rows.append(line.addr)
        return rows


async def _run(binary: Path, body, size: tuple[int, int]) -> None:
    app = TGDBApp(gdb_path="gdb", gdb_args=[*GDB_ISOLATION_ARGS, str(binary)], rc_file="NONE")
    async with app.run_test(size=size):
        await tgdb.debugger.wait_idle()
        await body(Session(app))
        app.gdb.terminate()


def run_session(binary: Path, body, *, size: tuple[int, int] = (200, 70)) -> None:
    """Run ``await body(session)`` against tgdb debugging *binary*."""

    async def guarded() -> None:
        await asyncio.wait_for(_run(binary, body, size), SESSION_TIMEOUT)

    asyncio.run(guarded())
