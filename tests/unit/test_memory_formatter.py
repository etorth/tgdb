"""Memory pane: MI block decoding and hex-dump layout."""

from tgdb.highlight_groups import HighlightGroups
from tgdb.memory_pane.formatter import MemoryFormatter, blocks_to_bytes


def block(begin: int, data: bytes) -> dict:
    return {"begin": hex(begin), "contents": data.hex()}


def test_blocks_to_bytes_concatenates_contiguous_blocks():
    base, raw = blocks_to_bytes([block(0x1000, b"\x01\x02"), block(0x1002, b"\x03")])
    assert base == 0x1000
    assert raw == [1, 2, 3]


def test_blocks_to_bytes_stops_at_gap():
    """Regression: bytes after an unreadable gap were shown at wrong addresses."""
    base, raw = blocks_to_bytes([block(0x1000, b"\x01\x02"), block(0x2000, b"\x09")])
    assert base == 0x1000
    assert raw == [1, 2]


def test_blocks_to_bytes_handles_empty_and_malformed():
    assert blocks_to_bytes([]) == (0, [])
    assert blocks_to_bytes([{"begin": "zz", "contents": "0a"}]) == (0, [10])


def lines(text) -> list[str]:
    return text.plain.split("\n")


def test_format_fills_exactly_the_body_height():
    fmt = MemoryFormatter()
    hl = HighlightGroups()
    data = bytes(range(fmt.bytes_per_row * 2))
    text = fmt.format(120, 5, [block(0x4000, data)], hl)
    rows = lines(text)
    assert len(rows) == 5
    assert "4000" in rows[0].lower()
    assert rows[2].strip() == ""


def test_format_empty_dump_has_no_extra_row():
    """Regression: an empty dump rendered a leading blank line (height + 1 rows)."""
    text = MemoryFormatter().format(80, 4, [], HighlightGroups())
    assert len(lines(text)) == 4
