"""
Public implementation of the memory-pane package.

``MemoryPane`` is a black-box hex/ASCII memory viewer. The caller constructs
the widget, injects one async memory-read callback, and updates the requested
address through ``set_address(...)``.
"""

from collections.abc import Callable

from rich.text import Text
from textual.widget import Widget

from ..async_util import supervise
from ..highlight_groups import HighlightGroups
from ..pane_base import PaneBase
from ..pane_base import fit_cells

_HEADER = "  Address           +0 +1 +2 +3  +4 +5 +6 +7  +8 +9 +A +B  +C +D +E +F  ASCII"


def _format_memory_rows(
    raw_blocks: list[dict], bytes_per_row: int = 16
) -> list[tuple[str, list[int], str]]:
    """Parse GDB MI memory blocks into (addr_str, bytes_list, ascii_str) rows."""
    all_bytes: list[int] = []
    base_addr: int = 0
    first = True
    for block in raw_blocks:
        if first:
            try:
                base_addr = int(block.get("begin", "0x0"), 16)
            except (ValueError, TypeError):
                base_addr = 0
            first = False
        contents = block.get("contents", "")
        for i in range(0, len(contents), 2):
            chunk = contents[i:i + 2]
            if len(chunk) == 2:
                try:
                    all_bytes.append(int(chunk, 16))
                except ValueError:
                    all_bytes.append(0)

    rows: list[tuple[str, list[int], str]] = []
    for row_idx in range(0, len(all_bytes), bytes_per_row):
        row_bytes = all_bytes[row_idx:row_idx + bytes_per_row]
        addr = base_addr + row_idx
        addr_str = f"0x{addr:016x}"
        ascii_str = "".join(
            chr(b) if 32 <= b < 127 else "." for b in row_bytes
        )
        rows.append((addr_str, row_bytes, ascii_str))
    return rows


class _MemoryContent(Widget):
    """Renders memory as hex + ASCII dump (no title row)."""

    DEFAULT_CSS = """
    _MemoryContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._rows: list[tuple[str, list[int], str]] = []
        self._address: str = ""
        self._byte_count: int = 64
        self._read_fn: Callable | None = None


    def set_rows(self, rows: list[tuple[str, list[int], str]]) -> None:
        self._rows = list(rows)
        self.refresh()


    def _format_row(self, addr_str: str, byte_list: list[int], ascii_str: str) -> str:
        groups = []
        for g in range(4):
            chunk = byte_list[g * 4:(g + 1) * 4]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            if len(chunk) < 4:
                hex_part += "   " * (4 - len(chunk))
            groups.append(hex_part)
        hex_section = "  ".join(groups)
        return f"{addr_str}  {hex_section}  |{ascii_str}|"


    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")

        # Header row
        result.append(
            fit_cells(_HEADER, width),
            style=self.hl.style("SelectedLineHighlight"),
        )

        data_height = height - 1
        for i, (addr_str, byte_list, ascii_str) in enumerate(self._rows[:data_height]):
            result.append("\n")
            line = self._format_row(addr_str, byte_list, ascii_str)
            result.append(fit_cells(line, width), style=self.hl.style("Normal"))

        shown = min(data_height, len(self._rows))
        remaining = data_height - shown
        for _ in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


class MemoryPane(PaneBase):
    """Render memory as a fixed-width hex/ASCII dump.

    Public interface
    ----------------
    ``MemoryPane(hl, **kwargs)``
        Create the widget with no active address.

    ``set_read_fn(fn)``
        Inject the async callback used to read raw memory blocks from GDB.

    ``set_address(addr, size=None)``
        Request a new memory region. Pass ``size=None`` (default) to size the
        request to the visible pane height; pass an explicit byte count to
        override that. The pane asynchronously fetches the bytes and refreshes
        itself when the request completes.
    """

    BYTES_PER_ROW = 16


    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        """Create an empty memory pane."""
        super().__init__(hl, **kwargs)
        self._content = _MemoryContent(hl)
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


    def _request_size(self) -> int:
        if self._explicit_size is not None:
            return self._explicit_size
        # Reserve one row for the header; default to 4 rows when the pane has
        # not been laid out yet so the first fetch has something to show.
        height = self._content.size.height or 0
        rows = max(4, height - 1)
        return rows * self.BYTES_PER_ROW


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
            rows = _format_memory_rows(raw)
            self._content.set_rows(rows)
