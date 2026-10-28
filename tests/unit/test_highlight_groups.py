"""Highlight groups: cgdb colour names and group aliases."""

import pytest
from rich.style import Style

from tgdb.highlight_groups import HighlightGroups, resolve_color


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Blue", "bright_blue"),
        ("DarkBlue", "blue"),
        ("Black", "black"),
        ("5", "color(5)"),
        ("-1", ""),
        ("none", ""),
        ("NONE", ""),
        ("puce", ""),
        ("--5", ""),
    ],
)
def test_resolve_color(name, expected):
    assert resolve_color(name) == expected


def test_set_via_cgdb_alias_and_style_parses():
    hl = HighlightGroups()
    hl.set("Arrow", fg="Green", attrs="bold")
    assert hl.get("ExecutingLineArrow").fg == "bright_green"
    Style.parse(hl.style("ExecutingLineArrow"))


def test_ctermbg_none_clears_colour():
    """Regression: uppercase NONE was stored as colour "none" (invalid Rich style)."""
    hl = HighlightGroups()
    hl.set("Search", fg="Red", bg="NONE", attrs="bold")
    assert hl.get("Search").bg is None
    Style.parse(hl.style("Search"))


def test_group_names_are_case_insensitive():
    hl = HighlightGroups()
    hl.set("comment", fg="Red")
    assert hl.get("Comment").fg == "bright_red"
