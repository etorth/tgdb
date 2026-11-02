"""Display-cell (not character) arithmetic for wide characters."""

from rich.cells import cell_len

from tgdb.command_line_bar.render import RenderMixin
from tgdb.file_dialog.view import _drop_left_cells
from tgdb.source_widget.content import expand_tabs_cells


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
