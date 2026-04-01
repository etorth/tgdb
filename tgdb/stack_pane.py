"""
Call stack pane widget.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .gdb_controller import Frame
from .highlight_groups import HighlightGroups
from .pane_utils import center_cells, fit_cells, frame_location


class StackPane(Widget):
    """Render the current call stack."""

    DEFAULT_CSS = """
    StackPane {
        width: 1fr;
        height: 1fr;
        min-width: 4;
        min-height: 2;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = True
        self._frames: list[Frame] = []
        self._current_level: int = 0

    def set_frames(self, frames: list[Frame], current_level: int = 0) -> None:
        self._frames = list(frames)
        self._current_level = current_level
        self.refresh()

    def _frame_text(self, frame: Frame) -> str:
        marker = ">" if frame.level == self._current_level else " "
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

        result.append(center_cells("Call Stack", width), style=self.hl.style("StatusLine"))

        visible_rows = max(0, height - 1)
        for frame in self._frames[:visible_rows]:
            result.append("\n")
            style = self.hl.style("SelectedLineHighlight") if frame.level == self._current_level else self.hl.style("Normal")
            result.append(fit_cells(self._frame_text(frame), width), style=style)

        remaining_rows = height - 1 - min(visible_rows, len(self._frames))
        for _ in range(max(0, remaining_rows)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))

        return result
