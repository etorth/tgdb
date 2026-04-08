"""
Call stack pane widget.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .gdb_controller import Frame
from .highlight_groups import HighlightGroups
from .pane_base import PaneBase
from .pane_utils import fit_cells, frame_location


class _StackContent(Widget):
    """Renders the call-stack frame list (no title row)."""

    DEFAULT_CSS = """
    _StackContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._frames: list[Frame] = []
        self._current_level: int = 0

    def set_frames(self, frames: list[Frame], current_level: int = 0) -> None:
        self._frames = list(frames)
        self._current_level = current_level
        self.refresh()

    def _frame_text(self, frame: Frame) -> str:
        if frame.level == self._current_level:
            marker = ">"
        else:
            marker = " "
        func = frame.func or "??"
        text = f"{marker} #{frame.level} {func}"
        location = frame_location(frame)
        if location:
            text += f"  {location}"
        return text

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")
        for i, frame in enumerate(self._frames[:height]):
            if i > 0:
                result.append("\n")
            if frame.level == self._current_level:
                style = self.hl.style("SelectedLineHighlight")
            else:
                style = self.hl.style("Normal")
            result.append(fit_cells(self._frame_text(frame), width), style=style)
        remaining = height - min(height, len(self._frames))
        for i in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


class StackPane(PaneBase):
    """Call-stack pane: title bar + scrollable frame list."""

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _StackContent(hl)

    def title(self) -> str:
        return "STACK"

    def compose(self):
        yield from super().compose()
        yield self._content

    def set_frames(self, frames: list[Frame], current_level: int = 0) -> None:
        self._content.set_frames(frames, current_level)
