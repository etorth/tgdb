"""Evaluations pane, alone and together with the locals pane."""

from .harness import run_session

WATCHES = ["a", "b", "arr[1]"]


def test_locals_and_watches_both_update(build):
    """Regression: both panes used ``-var-update *`` and stole each other's changes."""

    async def body(s):
        await s.layout("source", "gdb", "locals", "evaluate")
        await s.run_to("watch_locals.c", "MARK_START")
        for expr in WATCHES:
            assert await s.dbg.command(f"evaluate {expr}") is None
        await s.dbg.wait_idle()

        for _ in range(8):
            await s.cli("next")
            want = {expr: await s.gdb_value(expr) for expr in WATCHES}
            assert dict(s.dbg.watches()) == want
            assert s.locals_value("a") == want["a"]
            assert s.locals_value("b") == want["b"]

    run_session(build("watch_locals.c"), body)


def test_failing_watch_label_and_removal(build):
    """Regression: error label was parsed as Rich markup and could not be removed."""

    async def body(s):
        await s.layout("source", "gdb", "evaluate")
        await s.run_to("watch_locals.c", "MARK_START")
        await s.dbg.command("evaluate a")
        await s.dbg.command("evaluate nosuch[i]")
        await s.dbg.wait_idle()
        assert ("nosuch[i]", "<error>") in s.dbg.watches()

        await s.dbg.command("unevaluate 2")
        await s.dbg.wait_idle()
        assert [expr for expr, _value in s.dbg.watches()] == ["a"]

    run_session(build("watch_locals.c"), body)
