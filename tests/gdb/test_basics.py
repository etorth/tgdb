"""Smoke tests: startup, breakpoints, source pane, locals, workspace layout."""

from tgdb.workspace import PaneContainer

from .harness import run_session


def test_startup_loads_main_source(build):
    async def body(s):
        src = s.source_view()
        assert src.source_file is not None, "no source loaded at startup"
        assert src.source_file.path.endswith("basic.c")
        assert len(src.source_file.lines) > 20

    run_session(build("basic.c"), body)


def test_breakpoint_sets_executing_line(build):
    async def body(s):
        await s.run_to("basic.c", "MARK_MAIN_FIRST")
        src = s.source_view()
        assert src.source_file.path.endswith("basic.c")
        assert src.exe_line == s.line_of("basic.c", "MARK_MAIN_FIRST")

        await s.cli("next")
        assert src.exe_line == s.line_of("basic.c", "MARK_MAIN_FIRST") + 1

    run_session(build("basic.c"), body)


def test_step_into_function_and_up_down(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("basic.c", "MARK_ADD")
        assert s.locals_value("a") == "10"
        assert s.locals_value("b") == "20"

        await s.cli("up")
        assert s.source_view().exe_line != s.line_of("basic.c", "MARK_ADD")
        assert s.locals_value("x") == "10"
        assert s.locals_value("a") is None

        await s.cli("down")
        assert s.source_view().exe_line == s.line_of("basic.c", "MARK_ADD")
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
