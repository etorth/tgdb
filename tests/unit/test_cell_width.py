"""Display-cell arithmetic, clipping and scrolling of pane contents."""

import pytest
from rich.cells import cell_len
from textual.geometry import Size

from tgdb.command_line_bar.render import RenderMixin
from tgdb.file_dialog.view import _drop_left_cells
from tgdb.gdb_controller import Frame
from tgdb.highlight_groups import HighlightGroups
from tgdb.pane_base import scroll_to_show
from tgdb.source_widget.content import _SourceContent, expand_tabs_cells
from tgdb.source_widget.data import SourceFile
from tgdb.stack_pane.pane import _StackContent


class FakeInput(RenderMixin):
    def __init__(self, text: str, cursor: int) -> None:
        self._input_buf = text
        self._cursor_pos = cursor


def test_command_input_fits_width_with_wide_chars():
    """Regression: the input line scrolled and cropped by characters."""
    for cursor in (0, 3, 9):
        rendered = FakeInput("中文中文中文中文中", cursor)._render_input(10, "reverse")
        assert cell_len(rendered.plain) == 10, (cursor, rendered.plain)
    at_end = FakeInput("中文中文中文中文中", 9)._render_input(10, "reverse")
    assert at_end.plain.endswith(" ")  # cursor cell after the last character is visible


def test_tabs_expand_by_cells():
    """Regression: str.expandtabs counted the CJK character as one column."""
    assert expand_tabs_cells("中\tx", 4) == "中  x"
    assert expand_tabs_cells("ab\tx", 4) == "ab  x"
    assert cell_len(expand_tabs_cells("中文\tx", 8).split("x")[0]) == 8
    assert expand_tabs_cells("no tabs", 4) == "no tabs"


def test_drop_left_cells_marks_split_wide_char():
    assert _drop_left_cells("中a", 1) == "?a"
    assert _drop_left_cells("中a", 2) == "a"
    assert _drop_left_cells("ab", 5) == ""
    assert _drop_left_cells("ab", 0) == "ab"


def _highlighted(text, style):
    return [text.plain[span.start:span.end] for span in text.spans if span.style == style]


def test_hlsearch_matches_whole_line_when_scrolled():
    hl = HighlightGroups()
    content = _SourceContent(hl)
    content.source_file = SourceFile("x.c", ["    counter = counter + 1;"])
    content.hlsearch = True
    content._col_offset = 6  # the window starts at "unter = ..."
    content._search_pattern = "counter"
    line = content._build_line(0, content.source_file)
    assert _highlighted(line, hl.style("Search")) == ["unter", "counter"]

    content._search_pattern = "^counter"
    line = content._build_line(0, content.source_file)
    assert _highlighted(line, hl.style("Search")) == []


@pytest.mark.parametrize(
    ("top", "selected", "count", "height", "expected"),
    [
        (0, 3, 10, 5, 0),     # already visible
        (0, 7, 10, 5, 3),     # below: scroll just enough
        (4, 2, 10, 5, 2),     # above: scroll up to it
        (8, -1, 10, 5, 5),    # no selection: clamp to the end
        (3, 1, 2, 5, 0),      # fewer rows than height
    ],
)
def test_scroll_to_show(top, selected, count, height, expected):
    assert scroll_to_show(top, selected, count, height) == expected


class SizedStack(_StackContent):
    size = Size(20, 3)


def test_stack_pane_shows_selected_frame_below_height():
    content = SizedStack(HighlightGroups())
    content.set_frames([Frame(level=i, func=f"f{i}") for i in range(10)], current_level=6)
    rows = [row.rstrip() for row in content.render().plain.split("\n")]
    assert rows == ["  #4 f4", "  #5 f5", "> #6 f6"]
