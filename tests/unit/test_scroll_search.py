"""GDB pane scroll-mode search (n / N)."""

from collections import deque

from rich.text import Text

from tgdb.gdb_widget.scroll import ScrollMixin


class FakeScroll(ScrollMixin):
    """Just enough state for ScrollMixin's search over a fixed buffer."""

    def __init__(self, lines: list[str], height: int) -> None:
        self._lines = [Text(line) for line in lines]
        self._height = height
        self._scrollback = deque(maxlen=1000)
        self._scroll_offset = 0
        self._search_line = None
        self.ignorecase = False
        self.wrapscan = True


    def _all_lines(self) -> list[Text]:
        return self._lines


    def _visible_height(self) -> int:
        return self._height


    def refresh(self) -> None:
        pass


def make() -> FakeScroll:
    lines = ["x"] * 20
    for index in (2, 18, 19):
        lines[index] = "foo"
    return FakeScroll(lines, height=5)


def test_n_visits_matches_on_the_last_screen_then_wraps():
    """Regression: matches in the last screenful made ``n`` repeat forever."""
    view = make()
    seen = []
    for _ in range(4):
        assert view._do_search("foo", forward=True)
        seen.append(view._search_line)
    assert seen == [18, 19, 2, 18]
    assert view._scroll_offset == 0


def test_N_goes_backwards_from_last_match():
    view = make()
    view._do_search("foo", forward=True)
    view._do_search("foo", forward=True)
    assert view._search_line == 19
    view._do_search("foo", forward=False)
    assert view._search_line == 18


def test_manual_scroll_restarts_search_from_viewport():
    view = make()
    view._do_search("foo", forward=True)
    view._scroll_down()
    assert view._search_line is None
