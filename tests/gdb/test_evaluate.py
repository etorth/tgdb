"""Evaluations pane, alone and together with the locals pane."""

from .harness import run_session

WATCHES = ["a", "b", "arr[1]"]


def test_locals_and_watches_both_update(build):
    """Regression: both panes used ``-var-update *`` and stole each other's changes."""

    async def body(s):
        await s.layout("source", "gdb", "locals", "evaluate")
        await s.run_to("watch_locals.c", "MARK_START")
        for expr in WATCHES:
            await s.evaluate_pane.add_expression(expr)
        await s.settle()

        for _ in range(8):
            await s.cli("next")
            want = {expr: await s.gdb_value(expr) for expr in WATCHES}
            assert dict(s.watch_rows()) == want
            assert s.locals_value("a") == want["a"]
            assert s.locals_value("b") == want["b"]

    run_session(build("watch_locals.c"), body)


def test_failing_watch_label_and_removal(build):
    """Regression: error label was parsed as Rich markup and could not be removed."""

    async def body(s):
        await s.layout("source", "gdb", "evaluate")
        await s.run_to("watch_locals.c", "MARK_START")
        pane = s.evaluate_pane
        await pane.add_expression("a")
        await pane.add_expression("nosuch[i]")
        await s.settle()
        assert "nosuch[i] = <error>" in s.watch_labels()

        assert await pane.remove_expression(1) == "nosuch[i]"
        await s.settle()
        assert [name for name, _ in s.watch_rows()] == ["a"]

    run_session(build("watch_locals.c"), body)
