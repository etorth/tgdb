"""Locals pane reconciliation against real GDB."""

import pytest

from .harness import run_session


def test_register_locals_follow_selected_frame(build):
    """Regression: same-named register locals kept showing the old frame's value."""

    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("register_frames.c", "MARK_LEAF")
        if not str(getattr(s.collected_locals().get("k"), "addr", "")).startswith("register@"):
            pytest.skip("compiler did not keep k in a register")

        leaf_k = await s.gdb_value("k")
        assert s.locals_value("k") == leaf_k

        await s.cli("up")
        main_k = await s.gdb_value("k")
        assert main_k != leaf_k
        assert s.locals_value("k") == main_k

        await s.cli("down")
        assert s.locals_value("k") == leaf_k

    run_session(build("register_frames.c", "-O2"), body)


def test_reference_kept_across_finish_keeps_updating(build):
    """Regression: a root reported out of scope once was never refreshed again."""

    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("ref_finish.cpp", "MARK_B")
        await s.cli("finish")
        for _ in range(3):
            await s.cli("next")
            assert s.locals_value("v") == await s.gdb_value("v")

    run_session(build("ref_finish.cpp"), body)


def test_unconstructed_object_refreshes_once_constructed(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("unconstructed.cpp", "MARK_INSIDE_INIT")
        assert s.locals_value("s") is not None, "s should be listed while unconstructed"

        await s.cli(f"until {s.line_of('unconstructed.cpp', 'MARK_CONSTRUCTED')}")
        assert s.locals_value("s") == await s.gdb_eval("s") == '"xx"'
        assert "s" not in s.locals_placeholders()

    run_session(build("unconstructed.cpp"), body)


def test_locals_track_every_step(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("watch_locals.c", "MARK_START")
        for _ in range(8):
            await s.cli("next")
            for name in ("a", "b", "i"):
                assert s.locals_value(name) == await s.gdb_value(name), name

    run_session(build("watch_locals.c"), body)
