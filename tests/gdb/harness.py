"""Drive a real ``TGDBApp`` + GDB in-process for integration tests.

``run_session(binary, body)`` starts tgdb headless under Textual's test
driver, waits for GDB to come up, then awaits ``body(session)``.  The
``Session`` helpers wait for "idle" (inferior stopped and the locals
pipeline quiet) instead of sleeping fixed amounts, and compare pane contents
against values read back from GDB.

All knowledge of tgdb internals used by the tests lives in this module.
"""

import asyncio
from pathlib import Path

from textual.widgets import Tree

from tgdb.app import TGDBApp

SESSION_TIMEOUT = 120.0
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Ignore ~/.gdbinit, and never stop at the interactive debuginfod prompt.
GDB_ISOLATION_ARGS = ["-nh", "-iex", "set debuginfod enabled off"]


# First word of a GDB CLI command -> controller event that marks its completion.
_RESUME_COMMANDS = {
    "run", "r", "start", "next", "n", "step", "s", "finish", "fin",
    "until", "u", "advance", "continue", "c",
}
_FRAME_COMMANDS = {"up", "down", "frame", "f"}


class Session:
    def __init__(self, app: TGDBApp) -> None:
        self.app = app
        self.source_dir = FIXTURES
        self.stops = 0
        self.frame_changes = 0
        self.console = bytearray()
        self._wrap_callback("on_stopped", lambda *_: self._bump("stops"))
        self._wrap_callback("on_frame_changed", lambda *_: self._bump("frame_changes"))
        self._wrap_callback("on_console", self.console.extend)


    def _bump(self, counter: str) -> None:
        setattr(self, counter, getattr(self, counter) + 1)


    def _wrap_callback(self, attr: str, observe) -> None:
        original = getattr(self.app.gdb, attr)

        def observed(*args, **kwargs):
            observe(*args)
            return original(*args, **kwargs)

        setattr(self.app.gdb, attr, observed)


    def console_tail(self, lines: int = 25) -> str:
        text = self.console.decode("utf-8", errors="replace").replace("\r", "")
        return "\n".join(text.splitlines()[-lines:])

    # ------------------------------------------------------------------
    # source helpers
    # ------------------------------------------------------------------


    def line_of(self, filename: str, marker: str) -> int:
        """1-based line of the unique line in *filename* containing *marker*."""
        lines = (self.source_dir / filename).read_text(encoding="utf-8").splitlines()
        hits = [n for n, line in enumerate(lines, start=1) if marker in line]
        assert len(hits) == 1, f"marker {marker!r} found on lines {hits} of {filename}"
        return hits[0]

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------


    async def layout(self, *panes: str) -> None:
        """Replace the workspace with a vertical stack of *panes*."""
        import tgdb

        await tgdb.screen.close_all_panes()
        for _ in range(len(panes) - 1):
            await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.VERTICAL)
        for index, pane in enumerate(panes):
            await tgdb.screen.get_pane([index]).attach(getattr(tgdb.Pane, pane.upper()))
        await self.settle()


    async def add_pane(self, pane: str) -> None:
        """Append *pane* below the current vertical stack."""
        import tgdb

        await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.VERTICAL)
        root_items = self.app.query_one("#split-container").items
        await tgdb.screen.get_pane([len(root_items) - 1]).attach(getattr(tgdb.Pane, pane.upper()))
        await self.settle()

    # ------------------------------------------------------------------
    # driving GDB
    # ------------------------------------------------------------------


    def _busy(self) -> bool:
        gdb = self.app.gdb
        if gdb._inferior_running:
            return True
        if gdb._locals_cancel_token in gdb._pending:
            return True
        pane = self.app._locals_pane
        return pane is not None and pane._reconcile_active > 0


    async def settle(self, timeout: float = 30.0) -> None:
        """Wait until the inferior is stopped and pane refreshes are quiet."""
        await asyncio.sleep(0.3)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        quiet = 0
        while quiet < 8:
            if loop.time() > deadline:
                raise TimeoutError(f"tgdb did not settle within {timeout}s")
            quiet = 0 if self._busy() else quiet + 1
            await asyncio.sleep(0.1)


    async def _wait_for(self, counter: str, start: int, timeout: float = 30.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while getattr(self, counter) == start:
            if loop.time() > deadline:
                raise TimeoutError(
                    f"no {counter} event within {timeout}s; GDB console tail:\n{self.console_tail()}"
                )
            await asyncio.sleep(0.05)


    async def cli(self, command: str) -> None:
        """Type *command* into the GDB console and wait until it has taken effect.

        Commands that resume the inferior wait for the next stop; frame
        selection commands wait for the frame change; then wait for idle.
        """
        word = command.split()[0] if command.split() else ""
        stops, frames = self.stops, self.frame_changes
        self.app.gdb.send_input((command + "\r").encode())
        if word in _RESUME_COMMANDS:
            await self._wait_for("stops", stops)
        elif word in _FRAME_COMMANDS:
            await self._wait_for("frame_changes", frames)
        await self.settle()


    async def run_to(self, filename: str, marker: str) -> None:
        """Break on the line containing *marker* and ``run`` to it."""
        await self.cli(f"break {filename}:{self.line_of(filename, marker)}")
        await self.cli("run")


    async def gdb_value(self, expr: str) -> str:
        """Value of *expr* in the selected frame, formatted like a varobj."""
        gdb = self.app.gdb
        info = await gdb.var_create(expr)
        try:
            return str(info.get("value", ""))
        finally:
            if info.get("name"):
                await gdb.var_delete(info["name"])


    async def gdb_eval(self, expr: str) -> str:
        return await self.app.gdb._eval_expr_raw(expr)

    # ------------------------------------------------------------------
    # pane contents
    # ------------------------------------------------------------------


    @staticmethod
    def _rows(pane) -> list[tuple[str, str]]:
        """``(name, value)`` for each top-level row of a varobj tree pane."""
        if pane is None:
            return []
        rows: list[tuple[str, str]] = []
        for child in pane.query_one(Tree).root.children:
            text = child.label.plain
            left, _, value = text.partition(" = ")
            words = left.split()
            rows.append((words[-1] if words else "", value))
        return rows


    def locals_rows(self) -> list[tuple[str, str]]:
        return self._rows(self.app._locals_pane)


    def locals_value(self, name: str) -> str | None:
        return dict(self.locals_rows()).get(name)


    def locals_placeholders(self) -> list[str]:
        return [name for name, _addr in self.app._locals_pane._uninitialized_nodes]


    def collected_locals(self) -> dict:
        """Name -> LocalVariable as reported by the GDB-side collector."""
        return {var.name: var for var in self.app._current_locals}


    @property
    def evaluate_pane(self):
        return self.app._evaluate_pane


    def watch_labels(self) -> list[str]:
        pane = self.app._evaluate_pane
        return [child.label.plain for child in pane.query_one(Tree).root.children]


    def watch_rows(self) -> list[tuple[str, str]]:
        return self._rows(self.app._evaluate_pane)


    def source_view(self):
        return self.app._get_source_view()


async def _wait_for_startup(app: TGDBApp, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while app.gdb is None or app._get_source_view(mounted_only=True) is None:
        if loop.time() > deadline:
            raise TimeoutError("tgdb did not start")
        await asyncio.sleep(0.05)
    # on_mount finishes with an initial-location request; let it land.
    await asyncio.sleep(1.0)


async def _run(binary: Path, body, size: tuple[int, int]) -> None:
    app = TGDBApp(gdb_path="gdb", gdb_args=[*GDB_ISOLATION_ARGS, str(binary)], rc_file="NONE")
    async with app.run_test(size=size):
        await _wait_for_startup(app)
        await body(Session(app))
        app.gdb.terminate()


def run_session(binary: Path, body, *, size: tuple[int, int] = (200, 70)) -> None:
    """Run ``await body(session)`` against tgdb debugging *binary*."""

    async def guarded() -> None:
        await asyncio.wait_for(_run(binary, body, size), SESSION_TIMEOUT)

    asyncio.run(guarded())
