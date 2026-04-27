"""
Pluggable memory formatters for ``MemoryPane``.

A *memory formatter* is any object that exposes a ``format(width, height,
blocks, hl)`` method and, optionally, a ``header(width, height, hl)``
method. Both return a ``rich.text.Text`` (or ``None`` for "render nothing").

``MemoryFormatter`` is the default formatter, mirroring GDB-style hex+ASCII
dumps. It can be tuned via constructor arguments and then assigned through
``:set memoryformatter='MemoryFormatter(...)'``.

Users can swap in any object satisfying the contract — for example, a
custom formatter defined in a ``:python`` block — as long as it exposes the
required ``format`` callable.
"""

from typing import Any

from rich.text import Text

from ..highlight_groups import HighlightGroups
from ..pane_base import fit_cells


def is_valid_formatter(obj: Any) -> bool:
    """Return True when *obj* satisfies the memory-formatter contract."""
    if obj is None:
        return False
    fmt = getattr(obj, "format", None)
    return callable(fmt)


def blocks_to_bytes(blocks: list[dict]) -> tuple[int, list[int]]:
    """Flatten MI ``-data-read-memory-bytes`` blocks into (base_addr, bytes).

    The first block's ``begin`` field provides the base address; subsequent
    blocks' bytes are appended in order. Malformed entries contribute zero
    bytes / zero base.
    """
    base_addr = 0
    out: list[int] = []
    first = True
    for block in blocks or []:
        if first:
            try:
                base_addr = int(block.get("begin", "0x0"), 16)
            except (ValueError, TypeError):
                base_addr = 0
            first = False
        contents = block.get("contents", "") or ""
        for i in range(0, len(contents), 2):
            chunk = contents[i:i + 2]
            if len(chunk) != 2:
                continue
            try:
                out.append(int(chunk, 16))
            except ValueError:
                out.append(0)
    return base_addr, out


class MemoryFormatter:
    """Default hex+ASCII memory formatter.

    Constructor arguments
    ---------------------
    show_header
        When True, ``header()`` returns the column legend; when False it
        returns ``None`` and the pane renders no header.
    show_address
        When True each row is prefixed with the row's address.
    show_ascii
        When True an ASCII column is appended to each row.
    group_bytes
        Number of bytes per visual group (separated by a single space).
    row_groups
        Number of groups per row. Total bytes per row is
        ``group_bytes * row_groups``.
    """

    def __init__(
        self,
        show_header: bool = True,
        show_address: bool = True,
        show_ascii: bool = True,
        group_bytes: int = 4,
        row_groups: int = 4,
    ) -> None:
        self.show_header = bool(show_header)
        self.show_address = bool(show_address)
        self.show_ascii = bool(show_ascii)
        self.group_bytes = max(1, int(group_bytes))
        self.row_groups = max(1, int(row_groups))


    @property
    def bytes_per_row(self) -> int:
        return self.group_bytes * self.row_groups


    def header(self, width: int, height: int, hl: HighlightGroups) -> Text | None:
        """Return the legend row, or None when ``show_header`` is False.

        Per-byte labels are used when they fit; if offsets grow too wide
        the cadence is reduced (stride 2, 4, ..., up to one label per
        group) so labels never overrun their column or merge with the
        next group.
        """
        if not self.show_header:
            return None
        parts: list[str] = []
        if self.show_address:
            parts.append(f"  {'Address':<18}")
        body_legend = self._build_offset_legend()
        parts.append(body_legend)
        if self.show_ascii:
            parts.append("  ASCII")
        line = "".join(parts)
        text = Text(no_wrap=True, overflow="crop")
        text.append(
            fit_cells(line, max(1, width)),
            style=hl.style("SelectedLineHighlight"),
        )
        return text


    def _pick_offset_stride(self) -> int:
        """Choose the smallest power-of-two stride whose labels still fit."""
        total_bytes = self.bytes_per_row
        if total_bytes <= 0:
            return 1
        stride = 1
        while stride < total_bytes:
            last_offset = ((total_bytes - 1) // stride) * stride
            max_width = 1 + len(f"{last_offset:X}")
            slot = 3 * stride - 1
            if max_width <= slot:
                return stride
            stride *= 2
        return total_bytes


    def _build_offset_legend(self) -> str:
        """Render the per-byte legend using the chosen stride."""
        gb = self.group_bytes
        rg = self.row_groups
        total_bytes = gb * rg
        if total_bytes <= 0:
            return ""
        # Layout: byte k of group g starts at col g*(3*gb + 1) + k*3.
        per_group = 3 * gb - 1
        total_cols = rg * per_group + max(0, rg - 1) * 2
        buf = [" "] * total_cols
        stride = self._pick_offset_stride()
        for offset in range(0, total_bytes, stride):
            g, k = divmod(offset, gb)
            col = g * (3 * gb + 1) + k * 3
            label = f"+{offset:X}"
            for i, ch in enumerate(label):
                if col + i < total_cols:
                    buf[col + i] = ch
        return "".join(buf)


    def format(
        self,
        width: int,
        height: int,
        blocks: list[dict],
        hl: HighlightGroups,
    ) -> Text | None:
        """Render *blocks* as a hex+ASCII dump fitting the given size."""
        base_addr, raw = blocks_to_bytes(blocks)
        bpr = self.bytes_per_row
        rows: list[str] = []
        for off in range(0, len(raw), bpr):
            row_bytes = raw[off:off + bpr]
            rows.append(self._format_row(base_addr + off, row_bytes))

        body_height = max(0, height - (1 if self.show_header else 0))
        text = Text(no_wrap=True, overflow="crop")
        for i, line in enumerate(rows[:body_height]):
            if i:
                text.append("\n")
            text.append(
                fit_cells(line, max(1, width)),
                style=hl.style("Normal"),
            )
        # Pad the rest of the visible area with blank lines so the pane
        # background stays consistent.
        shown = min(body_height, len(rows))
        for _ in range(max(0, body_height - shown)):
            text.append("\n")
            text.append(" " * max(1, width), style=hl.style("Normal"))
        return text


    def _format_row(self, addr: int, row_bytes: list[int]) -> str:
        sections: list[str] = []
        if self.show_address:
            sections.append(f"0x{addr:016x}  ")
        groups: list[str] = []
        for g in range(self.row_groups):
            chunk = row_bytes[g * self.group_bytes:(g + 1) * self.group_bytes]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            if len(chunk) < self.group_bytes:
                hex_part += "   " * (self.group_bytes - len(chunk))
            groups.append(hex_part)
        sections.append("  ".join(groups))
        if self.show_ascii:
            ascii_str = "".join(
                chr(b) if 32 <= b < 127 else "." for b in row_bytes
            )
            sections.append(f"  |{ascii_str}|")
        return "".join(sections)


def build_formatter(expr: str, namespace: dict) -> tuple[Any, str | None]:
    """Evaluate *expr* in *namespace* and return ``(formatter, error)``.

    *namespace* is the persistent Python namespace (from ConfigParser).
    On success ``(obj, None)`` is returned; on failure ``(None, msg)``.
    The returned object is validated with :func:`is_valid_formatter`.
    """
    if not expr.strip():
        return MemoryFormatter(), None
    try:
        obj = eval(expr, dict(namespace))  # noqa: S307 -- user-supplied config
    except Exception as exc:
        return None, f"evaluation error: {exc}"
    if not is_valid_formatter(obj):
        return None, "object has no callable 'format' method"
    return obj, None
