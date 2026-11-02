"""GDB console resize and command-line bar editing."""

from tgdb.command_line_bar import CommandLineBar
from tgdb.command_line_bar.render import RenderMixin
from tgdb.gdb_widget.content import _GDBContent
from tgdb.highlight_groups import HighlightGroups


def test_gdb_pane_grow_restores_blank_rows():
    content = _GDBContent(HighlightGroups())
    content._init_pyte(6, 20)
    content._stream.feed(b"l1\r\n\r\nl3\r\nl4\r\nl5\r\n")
    content._init_pyte(2, 20)
    assert len(content._scrollback) == 4
    content._init_pyte(6, 20)
    assert len(content._scrollback) == 0
    rows = [content._screen.display[r].rstrip() for r in range(4)]
    assert rows == ["l1", "", "l3", "l4"]


def test_tab_after_history_recall_completes_recalled_line():
    calls = []

    def provider(arg_lead, buf, pos):
        calls.append(buf)
        return ["set"] if buf == "se" else []

    bar = CommandLineBar(HighlightGroups(), completion_provider=provider)
    bar._input_active = True
    bar._input_buf = "se"
    bar._cursor_pos = 2
    bar._add_to_history("set tabstop=4", max_size=100)
    bar.feed_key("tab", "")
    assert bar._input_buf == "set"
    bar.feed_key("up", "")
    assert bar._input_buf == "set tabstop=4"
    bar.feed_key("tab", "")
    assert calls == ["se", "set tabstop=4"]
    assert bar._input_buf == "set tabstop=4"


class FakeHeredoc(RenderMixin):
    def __init__(self) -> None:
        self._ml_history_recall = False
        self._ml_header = "python << EOF"
        self._ml_buf = []
        self._input_buf = "abc"
        self._cursor_pos = 1


def test_heredoc_cursor_drawn_at_cursor_position():
    last = FakeHeredoc()._render_ml_input(40, "reverse").plain.split("\n")[-1]
    assert last.startswith("  a▏bc")
