"""Smoke tests: startup, breakpoints, source pane, locals, workspace layout."""

from tgdb.workspace import PaneContainer

from .harness import run_session


def test_startup_loads_main_source(build):
    async def body(s):
        position = s.dbg.source_position()
        assert position is not None, "no source loaded at startup"
        assert position[0].endswith("basic.c")
        assert position[1] == 0, "nothing is executing before run"

    run_session(build("basic.c"), body)


def test_breakpoint_sets_executing_line(build):
    async def body(s):
        await s.run_to("basic.c", "MARK_MAIN_FIRST")
        path, line = s.dbg.source_position()
        assert path.endswith("basic.c")
        assert line == s.line_of("basic.c", "MARK_MAIN_FIRST")

        await s.cli("next")
        assert s.dbg.source_position()[1] == line + 1

    run_session(build("basic.c"), body)


def test_toggle_breakpoint_twice_clears_marker(build):
    async def body(s):
        line = s.line_of("basic.c", "MARK_MAIN_FIRST")
        await s.toggle_breakpoint(line)
        assert s.breakpoint_lines() == [line]

        # GDB sends no =breakpoint-deleted for an MI -break-delete.
        await s.toggle_breakpoint(line)
        assert s.breakpoint_lines() == []
        assert s.app.gdb.breakpoints == []

        await s.toggle_breakpoint(line)
        assert s.breakpoint_lines() == [line]

    run_session(build("basic.c"), body)


def test_step_into_function_and_up_down(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("basic.c", "MARK_ADD")
        add_line = s.line_of("basic.c", "MARK_ADD")
        assert s.locals_value("a") == "10"
        assert s.locals_value("b") == "20"

        await s.cli("up")
        assert s.dbg.source_position()[1] != add_line
        assert s.locals_value("x") == "10"
        assert s.locals_value("a") is None

        await s.cli("down")
        assert s.dbg.source_position()[1] == add_line
        assert s.locals_value("a") == "10"

    run_session(build("basic.c"), body)


def test_screen_api_layout(build):
    async def body(s):
        import tgdb

        root = s.app.query_one("#split-container", PaneContainer)
        assert len(root.items) == 2
        assert root.orientation == "vertical"

        await tgdb.screen.close_all_panes()
        root = s.app.query_one("#split-container", PaneContainer)
        assert len(root.items) == 1, "close_all_panes leaves one empty cell"

        await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.HORIZONTAL)
        assert len(root.items) == 2
        await tgdb.screen.get_pane([0]).attach(tgdb.Pane.SOURCE)
        await tgdb.screen.get_pane([1]).attach(tgdb.Pane.GDB)
        assert s.app._get_source_view().parent is root
        assert s.app._get_gdb_widget().parent is root

    run_session(build("basic.c"), body)


def test_debugger_api_command_and_errors(build):
    async def body(s):
        assert await s.dbg.command("set tabstop=3") is None
        assert s.app.cfg.tabstop == 3
        assert await s.dbg.command("set bogus")

        await s.run_to("basic.c", "MARK_MAIN_FIRST")
        assert await s.dbg.evaluate("1 + 2") == "3"
        try:
            await s.dbg.evaluate("no_such_symbol")
        except RuntimeError:
            pass
        else:
            raise AssertionError("evaluate() should raise for an unknown symbol")

        # "run" again asks "Start it from the beginning? (y or n)", so the
        # prompt never comes back: execute() must time out and show why.
        try:
            await s.dbg.execute("run", timeout=3)
        except TimeoutError as exc:
            assert "(y or n)" in str(exc)
        else:
            raise AssertionError("execute() should time out on a y/n question")
        s.app.gdb.send_input(b"n\r")
        await s.dbg.wait_idle()
        assert s.dbg.source_position()[1] == s.line_of("basic.c", "MARK_MAIN_FIRST")

    run_session(build("basic.c"), body)
