"""
Public implementation of the memory-pane package.

``MemoryPane`` is a black-box memory viewer. The caller constructs the widget,
injects one async memory-read callback, and updates the requested address
through ``set_address(...)``. Rendering is delegated to a *memory formatter*
plug-in (see :mod:`tgdb.memory_pane.formatter`). The default formatter
mirrors GDB-style hex+ASCII dumps; users can swap in their own through
``:set memoryformatter='MyFormatter(...)'``.
"""

from collections.abc import Callable

from rich.text import Text
from textual.widget import Widget

from ..async_util import supervise
from ..highlight_groups import HighlightGroups
from ..pane_base import PaneBase
from .formatter import MemoryFormatter, is_valid_formatter


class _MemoryContent(Widget):
    """Renders memory using the pane's current formatter."""

    DEFAULT_CSS = """
    _MemoryContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, formatter, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._blocks: list[dict] = []
        self._formatter = formatter
        self._read_fn: Callable | None = None


    def set_blocks(self, blocks: list[dict]) -> None:
        self._blocks = list(blocks or [])
        self.refresh()


    def set_formatter(self, formatter) -> None:
        self._formatter = formatter
        self.refresh()


    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        formatter = self._formatter
        result = Text(no_wrap=True, overflow="crop")

        header_text = None
        header_fn = getattr(formatter, "header", None)
        if callable(header_fn):
            try:
                header_text = header_fn(width, height, self.hl)
            except Exception:
                header_text = None

        if isinstance(header_text, Text) and len(header_text):
            result.append_text(header_text)
            result.append("\n")
            header_lines = header_text.plain.count("\n") + 1
            body_height = max(1, height - header_lines)
        elif isinstance(header_text, str) and header_text:
            result.append(
                header_text,
                style=self.hl.style("SelectedLineHighlight"),
            )
            result.append("\n")
            header_lines = header_text.count("\n") + 1
            body_height = max(1, height - header_lines)
        else:
            body_height = height

        body: Text | str | None = None
        try:
            body = formatter.format(width, body_height, self._blocks, self.hl)
        except Exception:
            body = None
        if isinstance(body, Text):
            result.append_text(body)
        elif isinstance(body, str) and body:
            result.append(body, style=self.hl.style("Normal"))
        return result


class MemoryPane(PaneBase):
    """Render memory using a pluggable formatter.

    Public interface
    ----------------
    ``MemoryPane(hl, *, formatter=None, **kwargs)``
        Create the widget with no active address. ``formatter`` defaults to
        :class:`MemoryFormatter`.

    ``set_read_fn(fn)``
        Inject the async callback used to read raw memory blocks from GDB.

    ``set_address(addr, size=None)``
        Request a new memory region. Pass ``size=None`` (default) to size the
        request to the visible pane height; pass an explicit byte count to
        override that.

    ``set_formatter(formatter)``
        Swap the formatter at runtime. Falls back to :class:`MemoryFormatter`
        when *formatter* fails the contract check.
    """

    BYTES_PER_ROW_FALLBACK = 16


    def __init__(self, hl: HighlightGroups, *, formatter=None, **kwargs) -> None:
        """Create an empty memory pane bound to *formatter*."""
        super().__init__(hl, **kwargs)
        if not is_valid_formatter(formatter):
            formatter = MemoryFormatter()
        self._formatter = formatter
        self._content = _MemoryContent(hl, formatter)
        self._current_address: str = ""
        self._explicit_size: int | None = None
        self._read_fn: Callable | None = None


    def title(self) -> str:
        return "MEMORY"


    def compose(self):
        yield from super().compose()
        yield self._content


    def set_read_fn(self, fn: Callable) -> None:
        """Install the async callback used to fetch raw memory bytes."""
        self._read_fn = fn
        self._content._read_fn = fn


    def set_formatter(self, formatter) -> None:
        """Swap the formatter; invalid objects fall back to the default."""
        if not is_valid_formatter(formatter):
            formatter = MemoryFormatter()
        self._formatter = formatter
        self._content.set_formatter(formatter)
        if self._current_address and self._explicit_size is None:
            supervise(
                self._fetch(self._current_address, self._request_size()),
                name="memory-formatter-resize",
            )


    def _bytes_per_row(self) -> int:
        bpr = getattr(self._formatter, "bytes_per_row", None)
        if isinstance(bpr, int) and bpr > 0:
            return bpr
        return self.BYTES_PER_ROW_FALLBACK


    def _request_size(self) -> int:
        if self._explicit_size is not None:
            return self._explicit_size
        # Reserve one row for the header; default to 4 rows when the pane has
        # not been laid out yet so the first fetch has something to show.
        height = self._content.size.height or 0
        rows = max(4, height - 1)
        return rows * self._bytes_per_row()


    def set_address(self, addr: str, size: int | None = None) -> None:
        """Request a new memory dump starting at *addr*."""
        self._current_address = addr
        self._explicit_size = size
        supervise(
            self._fetch(addr, self._request_size()), name="memory-fetch",
        )


    def refresh_memory(self) -> None:
        """Re-fetch the current region (used after each GDB stop)."""
        if not self._current_address:
            return
        supervise(
            self._fetch(self._current_address, self._request_size()),
            name="memory-refresh",
        )


    def on_resize(self, event) -> None:
        # When the user grows the pane, refetch so newly-visible rows are
        # populated with real data instead of blank padding.
        if self._current_address and self._explicit_size is None:
            supervise(
                self._fetch(self._current_address, self._request_size()),
                name="memory-resize",
            )


    async def _fetch(self, addr: str, size: int) -> None:
        if self._read_fn:
            try:
                raw = await self._read_fn(addr, size)
            except Exception:
                raw = []
            self._content.set_blocks(raw)

