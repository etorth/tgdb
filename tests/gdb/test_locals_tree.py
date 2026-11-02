"""Locals pane tree behaviour: shadowing, placeholders, dynamic children, expansion."""

from textual.widgets import Tree

from .harness import run_session


def test_shadowed_outer_row_keeps_updating(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("shadow.cpp", "MARK_WRITE_OUTER")
        assert s.locals_values("a") == ["0", "12"]

        await s.cli("next")  # *p = 8 writes the shadowed outer a
        assert s.locals_values("a") == ["8", "12"]

        await s.cli(f"until {s.line_of('shadow.cpp', 'MARK_AFTER_BLOCK')}")
        assert s.locals_values("a") == ["8"]
        await s.cli("next")
        assert s.locals_values("a") == ["9"]

    run_session(build("shadow.cpp"), body)


def test_anonymous_namespace_placeholder_is_promoted(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("anon_namespace.cpp", "MARK_MIDDLE")
        assert len(s.locals_values("a")) == 2
        assert "a" in s.locals_placeholders(), "shadowed outer A cannot be created by name"
        inner = await s.expand_local("a", 1)
        assert ("a", "1") in inner

        await s.cli(f"until {s.line_of('anon_namespace.cpp', 'MARK_OUTER')}")
        assert len(s.locals_values("a")) == 1
        assert "a" not in s.locals_placeholders()
        outer = await s.expand_local("a")
        assert ("a", "0") in outer
        assert ("s", '"0"') in outer

    run_session(build("anon_namespace.cpp"), body)


def test_variant_alternative_switch_while_expanded(build):
    """Regression guard: a dynamic child-shape change must not crash GDB."""

    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("variant.cpp", "MARK_TO_VECTOR")
        children = await s.expand_local("v")
        assert any(value == '"string"' for _exp, value in children), children

        await s.cli("next")  # string -> vector
        children = await s.expand_local("v")
        assert [value for _exp, value in children] == ["1", "2", "3"]

        for _ in range(2):  # vector -> int -> float
            await s.cli("next")
            assert s.locals_value("v") is not None
            assert await s.dbg.evaluate("1") == "1", "GDB must still respond"

    run_session(build("variant.cpp"), body)


def test_recursion_levels_show_their_own_locals(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.cli(f"break recursion.c:{s.line_of('recursion.c', 'MARK_RECURSE')} if r == 7")
        await s.cli("run")
        for command in ["", "up", "up", "down", "down"]:
            if command:
                await s.cli(command)
            for name in ("r", "a"):
                assert s.locals_value(name) == await s.gdb_value(name), (command, name)
        assert s.locals_value("r") == "7"

    run_session(build("recursion.c"), body)


def test_expanded_vector_follows_growth(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("containers.cpp", "MARK_PUSH")
        await s.expand_local("v")
        for _ in range(3):
            await s.cli("next")
        children = s.local_children("v")
        assert [value for _exp, value in children] == ["1", "2", "3"]

    run_session(build("containers.cpp"), body)


def test_expansion_restored_after_up_down(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("containers.cpp", "MARK_USE")
        assert await s.expand_local("pt") == [("x", "3"), ("y", "4")]

        await s.cli("up")
        assert s.locals_value("pt") is None
        await s.cli("down")
        assert s.local_children("pt") == [("x", "3"), ("y", "4")]

    run_session(build("containers.cpp"), body)


def test_slot_reused_by_other_type_is_recreated(build):
    """``int x`` and ``float x`` in the same stack slot: show the float."""
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("locals_changes.cpp", "MARK_INT")
        addr = s.collected_locals()["x"].addr
        await s.cli(f"break locals_changes.cpp:{s.line_of('locals_changes.cpp', 'MARK_FLOAT')}")
        await s.cli("continue")
        assert s.collected_locals()["x"].addr == addr, "slot not reused; test is moot"
        assert s.locals_value("x") == await s.gdb_value("x") == "2.5"

    run_session(build("locals_changes.cpp"), body)


def test_expanded_vector_follows_shrink(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("locals_changes.cpp", "MARK_POP")
        assert len(await s.expand_local("v")) == 5
        await s.cli("next")
        children = s.local_children("v")
        assert children is not None, "vector was re-created collapsed"
        assert [value for _exp, value in children] == ["1", "2", "3", "4"]

    run_session(build("locals_changes.cpp"), body)


def test_member_dynamic_type_change_keeps_child(build):
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.cli("set print object on")
        await s.run_to("locals_changes.cpp", "MARK_RETARGET")
        assert "p" in [exp for exp, _value in await s.expand_local("h")]
        await s.cli("next")
        children = s.local_children("h")
        assert children is not None and "p" in [exp for exp, _value in children], children

    run_session(build("locals_changes.cpp"), body)


def test_keys_in_focused_locals_tree(build):
    """Regression: the pane's row dict shadowed Textual's ``_bindings``, so
    any key pressed in the focused Locals tree crashed tgdb."""
    async def body(s):
        await s.layout("source", "gdb", "locals")
        await s.run_to("containers.cpp", "MARK_USE")
        s.app._locals_pane.query_one(Tree).focus()
        await s.pilot.press("e", "down", "up")
        await s.dbg.wait_idle()
        assert s.app.is_running
        assert s.locals_value("pt") is not None

    run_session(build("containers.cpp"), body)
