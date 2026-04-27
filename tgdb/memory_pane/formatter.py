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


_BYTE_FORMATS = {
    "hex": (2, "{:02x}"),
    "bin": (8, "{:08b}"),
    "oct": (3, "{:03o}"),
    "dec": (3, "{:3d}"),
}


_GROUP_HL = "MemoryGroup"


def _reverse_bits(b: int) -> int:
    """Bit-reverse a byte (0b1000_1000 -> 0b0001_0001)."""
    b &= 0xff
    r = 0
    for _ in range(8):
        r = (r << 1) | (b & 1)
        b >>= 1
    return r


class MemoryFormatter:
    """Default memory formatter.

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
    reverse_groups
        When True the group order in each row is mirrored — the right-most
        group then holds the lowest-offset bytes.
    reverse_bytes
        When True the byte order inside each group is mirrored.
    reverse_bits
        When True every byte's bits are reversed (0x88 -> 0x11) before
        rendering. Affects both the hex/bin/oct/dec column and the ASCII
        column.
    byte_format
        ``'hex'`` | ``'bin'`` | ``'oct'`` | ``'dec'``. All bytes are
        rendered with the format's fixed cell width (2/8/3/3 cells).
    """

    def __init__(
        self,
        show_header: bool = True,
        show_address: bool = True,
        show_ascii: bool = True,
        group_bytes: int = 4,
        row_groups: int = 4,
        reverse_groups: bool = False,
        reverse_bytes: bool = False,
        reverse_bits: bool = False,
        byte_format: str = "hex",
    ) -> None:
        self.show_header = bool(show_header)
        self.show_address = bool(show_address)
        self.show_ascii = bool(show_ascii)
        self.group_bytes = max(1, int(group_bytes))
        self.row_groups = max(1, int(row_groups))
        self.reverse_groups = bool(reverse_groups)
        self.reverse_bytes = bool(reverse_bytes)
        self.reverse_bits = bool(reverse_bits)
        fmt = str(byte_format).lower()
        if fmt not in _BYTE_FORMATS:
            raise ValueError(
                f"byte_format must be one of {sorted(_BYTE_FORMATS)}, got {byte_format!r}"
            )
        self.byte_format = fmt
        self._cell_w, self._cell_fmt = _BYTE_FORMATS[fmt]


    def _group_style(self, base: str, hl: HighlightGroups) -> str:
        gs = hl.style(_GROUP_HL)
        if not gs:
            return base
        return f"{base} {gs}" if base else gs


    @property
    def bytes_per_row(self) -> int:
        return self.group_bytes * self.row_groups


    def header(self, width: int, height: int, hl: HighlightGroups) -> Text | None:
        """Return the legend row, or None when ``show_header`` is False.

        Per-byte labels are used when they fit; if offsets grow too wide
        the cadence is reduced (stride 2, 4, ..., up to one label per
        group) so labels never overrun their column or merge with the
        next group. Each group region is tinted with a subtle alternating
        background so columns line up with the body rows.
        """
        if not self.show_header:
            return None
        base = hl.style("SelectedLineHighlight")
        text = Text(no_wrap=True, overflow="crop", style=base)
        if self.show_address:
            text.append(f"{'Address':<18}  ")
        text.append(self._build_offset_legend())
        if self.show_ascii:
            text.append("  ASCII")
        text.truncate(max(1, width), pad=True)
        return text


    def _pick_offset_stride(self) -> int:
        """Choose the smallest power-of-two stride whose labels still fit."""
        total_bytes = self.bytes_per_row
        if total_bytes <= 0:
            return 1
        w = self._cell_w
        stride = 1
        while stride < total_bytes:
            last_offset = ((total_bytes - 1) // stride) * stride
            max_width = 1 + len(f"{last_offset:X}")
            slot = (w + 1) * stride - 1
            if max_width <= slot:
                return stride
            stride *= 2
        return total_bytes


    def _byte_col(self, g_disp: int, k_disp: int) -> int:
        """Display column where the byte cell at (g_disp, k_disp) starts."""
        gb = self.group_bytes
        w = self._cell_w
        group_width = gb * w + (gb - 1)
        return g_disp * (group_width + 2) + k_disp * (w + 1)


    def _logical_offset(self, g_disp: int, k_disp: int) -> int:
        gb = self.group_bytes
        rg = self.row_groups
        g_log = (rg - 1 - g_disp) if self.reverse_groups else g_disp
        k_log = (gb - 1 - k_disp) if self.reverse_bytes else k_disp
        return g_log * gb + k_log


    def _build_offset_legend(self) -> str:
        """Render the per-byte legend using the chosen stride."""
        gb = self.group_bytes
        rg = self.row_groups
        w = self._cell_w
        total_bytes = gb * rg
        if total_bytes <= 0:
            return ""
        group_width = gb * w + (gb - 1)
        total_cols = rg * group_width + max(0, rg - 1) * 2
        buf = [" "] * total_cols
        stride = self._pick_offset_stride()
        slot_width = stride * w + (stride - 1)
        for g_disp in range(rg):
            for k_disp in range(gb):
                offset = self._logical_offset(g_disp, k_disp)
                if offset % stride != 0:
                    continue
                col = self._byte_col(g_disp, k_disp)
                label = f"+{offset:X}"
                col += max(0, slot_width - len(label))
                for i, ch in enumerate(label):
                    if 0 <= col + i < total_cols:
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
        base = hl.style("Normal")
        body_height = max(0, height)
        text = Text(no_wrap=True, overflow="crop")
        emitted = 0
        for off in range(0, len(raw), bpr):
            if emitted >= body_height:
                break
            if emitted:
                text.append("\n")
            row = self._row_text(base_addr + off, raw[off:off + bpr], base, hl)
            row.truncate(max(1, width), pad=True)
            text.append_text(row)
            emitted += 1
        for _ in range(max(0, body_height - emitted)):
            text.append("\n")
            text.append(" " * max(1, width), style=base)
        return text


    def _header_lines(self) -> int:
        return 1 if self.show_header else 0


    def _row_text(self, addr: int, row_bytes: list[int], base: str, hl: HighlightGroups) -> Text:
        text = Text(no_wrap=True, overflow="crop", style=base)
        if self.show_address:
            text.append(f"0x{addr:016x}  ")
        gb = self.group_bytes
        rg = self.row_groups
        w = self._cell_w
        group_style = self._group_style(base, hl)
        for g_disp in range(rg):
            cells: list[str] = []
            for k_disp in range(gb):
                logical = self._logical_offset(g_disp, k_disp)
                if logical < len(row_bytes):
                    b = row_bytes[logical]
                    if self.reverse_bits:
                        b = _reverse_bits(b)
                    cells.append(self._cell_fmt.format(b))
                else:
                    cells.append(" " * w)
            text.append(" ".join(cells), style=group_style)
            if g_disp < rg - 1:
                text.append("  ")
        if self.show_ascii:
            ascii_str = "".join(
                chr(b) if 32 <= b < 127 else "." for b in row_bytes
            )
            text.append(f"  |{ascii_str}|")
        return text


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
