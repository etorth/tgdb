"""
Public implementation of the thread-pane package.

``ThreadPane`` is a black-box widget for showing the debugger's thread list.
Construct it with the shared highlight palette, then publish parsed thread
snapshots with ``set_threads(...)``.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from ..gdb_controller import ThreadInfo
from ..highlight_groups import HighlightGroups
from ..pane_chrome import PaneBase
from ..pane_utils import fit_cells, frame_location


class _ThreadContent(Widget):
    """Renders the thread list (no title row)."""

    DEFAULT_CSS = """
    _ThreadContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._threads: list[ThreadInfo] = []

    def set_threads(self, threads: list[ThreadInfo]) -> None:
        self._threads = list(threads)
        self.refresh()

    def _thread_text(self, thread: ThreadInfo) -> str:
        if thread.is_current:
            marker = ">"
        else:
            marker = " "
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
        for i, thread in enumerate(self._threads[:height]):
            if i > 0:
                result.append("\n")
            if thread.is_current:
                style = self.hl.style("SelectedLineHighlight")
            else:
                style = self.hl.style("Normal")
            result.append(fit_cells(self._thread_text(thread), width), style=style)
        remaining = height - min(height, len(self._threads))
        for i in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


class ThreadPane(PaneBase):
    """Render the current thread list as a read-only pane.

    Public interface
    ----------------
    ``ThreadPane(hl, **kwargs)``
        Create the widget.

    ``set_threads(threads)``
        Replace the visible thread snapshot. The pane highlights whichever
        thread is marked current in the incoming data.
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        """Create an empty thread pane."""
        super().__init__(hl, **kwargs)
        self._content = _ThreadContent(hl)

    def title(self) -> str:
        return "Threads"

    def compose(self):
        yield from super().compose()
        yield self._content

    def set_threads(self, threads: list[ThreadInfo]) -> None:
        """Publish the latest debugger thread snapshot."""
        self._content.set_threads(threads)
