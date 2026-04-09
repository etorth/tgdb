"""
File browser pane — shows GDB source files, allows navigating and opening them.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from .highlight_groups import HighlightGroups
from .pane_base import PaneBase
from .pane_utils import fit_cells


class OpenSourceFile(Message):
    """Posted when the user selects a source file to open."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path


class _FileBrowserContent(Widget):
    """Renders the file list with keyboard/mouse navigation."""

    DEFAULT_CSS = """
    _FileBrowserContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = True
        self._files: list[str] = []
        self._filtered: list[str] = []
        self._selected: int = 0
        self._scroll_top: int = 0
        self._await_g: bool = False
        self._filter_mode: bool = False
        self._filter_buf: str = ""

    def set_files(self, files: list[str]) -> None:
        self._files = list(files)
        self._apply_filter()
        self._selected = max(0, min(self._selected, len(self._filtered) - 1))
        self._ensure_visible()
        self.refresh()

    def _apply_filter(self) -> None:
        if self._filter_buf:
            self._filtered = [f for f in self._files if self._filter_buf.lower() in f.lower()]
        else:
            self._filtered = list(self._files)

    def _ensure_visible(self) -> None:
        height = max(1, self.size.height or 1)
        if self._selected < self._scroll_top:
            self._scroll_top = self._selected
        elif self._selected >= self._scroll_top + height:
            self._scroll_top = self._selected - height + 1

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")
        visible = self._filtered[self._scroll_top:self._scroll_top + height]
        for i, path in enumerate(visible):
            if i > 0:
                result.append("\n")
            abs_idx = self._scroll_top + i
            if abs_idx == self._selected:
                style = self.hl.style("SelectedLineHighlight")
            else:
                style = self.hl.style("Normal")
            result.append(fit_cells(path, width), style=style)
        if self._filter_mode:
            # Show filter prompt on last row
            prompt = fit_cells(f"/{self._filter_buf}", width)
            if len(visible) < height:
                result.append("\n")
                result.append(prompt, style=self.hl.style("SelectedLineHighlight"))
            # replace last line with prompt
        remaining = height - len(visible)
        for i in range(max(0, remaining - (1 if self._filter_mode else 0))):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result

    def on_key(self, event: events.Key) -> None:
        if self._filter_mode:
            self._handle_filter_key(event)
            return

        key = event.key
        if key in ("j", "down"):
            self._selected = min(len(self._filtered) - 1, self._selected + 1) if self._filtered else 0
            self._ensure_visible()
            self._await_g = False
            self.refresh()
            event.stop()
        elif key in ("k", "up"):
            self._selected = max(0, self._selected - 1)
            self._ensure_visible()
            self._await_g = False
            self.refresh()
            event.stop()
        elif key == "G":
            self._selected = max(0, len(self._filtered) - 1)
            self._ensure_visible()
            self._await_g = False
            self.refresh()
            event.stop()
        elif key == "g":
            if self._await_g:
                self._selected = 0
                self._scroll_top = 0
                self._await_g = False
                self.refresh()
            else:
                self._await_g = True
            event.stop()
        elif key in ("enter", "return"):
            if self._filtered:
                self.post_message(OpenSourceFile(self._filtered[self._selected]))
            self._await_g = False
            event.stop()
        elif key == "slash":
            self._filter_mode = True
            self._filter_buf = ""
            self._await_g = False
            self.refresh()
            event.stop()
        elif key == "escape":
            self._filter_buf = ""
            self._apply_filter()
            self._selected = 0
            self._scroll_top = 0
            self._await_g = False
            self.refresh()
            event.stop()
        else:
            self._await_g = False

    def _handle_filter_key(self, event: events.Key) -> None:
        key = event.key
        if key == "escape":
            self._filter_mode = False
            self._filter_buf = ""
            self._apply_filter()
            self._selected = 0
            self._scroll_top = 0
            self.refresh()
            event.stop()
        elif key in ("enter", "return"):
            self._filter_mode = False
            self._apply_filter()
            self._selected = 0
            self._scroll_top = 0
            self.refresh()
            event.stop()
        elif key == "backspace":
            self._filter_buf = self._filter_buf[:-1]
            self._apply_filter()
            self._selected = 0
            self._scroll_top = 0
            self.refresh()
            event.stop()
        elif event.character and event.character.isprintable():
            self._filter_buf += event.character
            self._apply_filter()
            self._selected = 0
            self._scroll_top = 0
            self.refresh()
            event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        row = int(event.y)
        idx = self._scroll_top + row
        if 0 <= idx < len(self._filtered):
            self._selected = idx
            self._ensure_visible()
            self.refresh()
            self.post_message(OpenSourceFile(self._filtered[self._selected]))
        event.stop()


class FileBrowserPane(PaneBase):
    """File browser pane: title bar + navigable file list."""

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _FileBrowserContent(hl)

    def title(self) -> str:
        return "FILES"

    def compose(self):
        yield from super().compose()
        yield self._content

    def set_files(self, files: list[str]) -> None:
        self._content.set_files(files)

    def on_focus(self) -> None:
        self._content.focus()
