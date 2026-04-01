"""
Thread list pane widget.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .gdb_controller import ThreadInfo
from .highlight_groups import HighlightGroups
from .pane_utils import center_cells, fit_cells, frame_location


class ThreadPane(Widget):
    """Render all known threads and highlight the current one."""

    DEFAULT_CSS = """
    ThreadPane {
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
        self._threads: list[ThreadInfo] = []

    def set_threads(self, threads: list[ThreadInfo]) -> None:
        self._threads = list(threads)
        self.refresh()

    def _thread_text(self, thread: ThreadInfo) -> str:
        marker = ">" if thread.is_current else " "
        state = thread.state or "unknown"
        text = f"{marker} {thread.id} {state}"
        if thread.name:
            text += f" {thread.name}"
        if thread.frame is not None:
            func = thread.frame.func or "??"
            text += f"  {func}"
            location = frame_location(thread.frame)
            if location:
                text += f"  {location}"
        elif thread.target_id:
            text += f"  {thread.target_id}"
        if thread.core:
            text += f" [core {thread.core}]"
        return text

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")

        result.append(center_cells("Threads", width), style=self.hl.style("StatusLine"))

        visible_rows = max(0, height - 1)
        for thread in self._threads[:visible_rows]:
            result.append("\n")
            style = self.hl.style("SelectedLineHighlight") if thread.is_current else self.hl.style("Normal")
            result.append(fit_cells(self._thread_text(thread), width), style=style)

        remaining_rows = height - 1 - min(visible_rows, len(self._threads))
        for _ in range(max(0, remaining_rows)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))

        return result
