"""SourceView / GDBWidget forward their content widget's state automatically."""

import pytest

from tgdb.gdb_widget import GDBWidget
from tgdb.highlight_groups import HighlightGroups
from tgdb.source_widget import SourceView


@pytest.fixture
def src() -> SourceView:
    return SourceView(HighlightGroups(), id="src-pane")


@pytest.fixture
def gdb() -> GDBWidget:
    return GDBWidget(HighlightGroups(), id="gdb-pane")


@pytest.mark.parametrize("name", ["source_file", "_search_active", "sel_line", "tabstop", "_show_logo"])
def test_source_state_set_on_wrapper_reaches_content(src, name):
    """Regression: names missing from a hand-kept list were stored on the wrapper."""
    marker = object()
    setattr(src, name, marker)
    assert getattr(src._content, name) is marker
    assert name not in vars(src)
    assert getattr(src, name) is marker


def test_gdb_property_and_callback_reach_content(gdb):
    gdb.max_scrollback = 7
    assert gdb._content._scrollback.maxlen == 7

    def send(data):
        return None

    gdb.send_to_gdb = send
    assert gdb._content.send_to_gdb is send


def test_pane_own_and_textual_attributes_stay_on_wrapper(src, gdb):
    for pane in (src, gdb):
        # Set by PaneBase itself, or by Textual's Widget.__init__ on both widgets.
        for name in ("hl", "can_focus", "_title_bar", "_size", "_id"):
            assert name not in pane._delegated_names, name

    hl = HighlightGroups()
    src.hl = hl
    assert vars(src)["hl"] is hl
    assert src._content.hl is not hl


def test_title_bar_color_method_survives_setting_color(src):
    """``color`` is both a PaneBase method and a content option."""
    src.color = False
    assert src._content.color is False
    assert src.color() == "CommandLine"
