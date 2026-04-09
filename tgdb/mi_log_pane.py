"""
MI log pane — shows raw GDB/MI output for debugging.
"""

from __future__ import annotations

from collections import deque

from rich.text import Text
from textual.widget import Widget

from .highlight_groups import HighlightGroups
from .pane_base import PaneBase
from .pane_utils import fit_cells


class _MILogContent(Widget):
    """Renders raw MI log lines (no title row)."""

    DEFAULT_CSS = """
    _MILogContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._lines: deque[str] = deque(maxlen=500)

    def append_line(self, text: str) -> None:
        for line in text.split("\n"):
            self._lines.append(line)
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        lines = list(self._lines)
        visible = lines[-height:] if len(lines) > height else lines
        result = Text(no_wrap=True, overflow="crop")
        for i, line in enumerate(visible):
            if i > 0:
                result.append("\n")
            result.append(fit_cells(line, width), style=self.hl.style("Normal"))
        remaining = height - len(visible)
        for i in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


class MILogPane(PaneBase):
    """MI log pane: title bar + scrollable raw MI output."""

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _MILogContent(hl)

    def title(self) -> str:
        return "MI LOG"

    def compose(self):
        yield from super().compose()
        yield self._content

    def append_line(self, text: str) -> None:
        self._content.append_line(text)
