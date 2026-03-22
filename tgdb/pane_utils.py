"""
Shared helpers for pane rendering.
"""
from __future__ import annotations

import os

from rich.cells import cell_len, split_graphemes

from .gdb_controller import Frame


def fit_cells(text: str, width: int) -> str:
    """Clip text to a given display-cell width and right-pad the remainder."""
    if width <= 0:
        return ""
    used = 0
    parts: list[str] = []
    graphemes, _ = split_graphemes(text)
    for start, end, grapheme_width in graphemes:
        if used + grapheme_width > width:
            break
        parts.append(text[start:end])
        used += grapheme_width
    return "".join(parts) + (" " * max(0, width - used))


def center_cells(text: str, width: int) -> str:
    """Center text within a given display-cell width, clipping if needed."""
    if width <= 0:
        return ""

    text_width = cell_len(text)
    if text_width >= width:
        return fit_cells(text, width)

    pad = width - text_width
    left = pad // 2
    right = pad - left
    return (" " * left) + text + (" " * right)


def frame_location(frame: Frame | None) -> str:
    """Return a short human-readable location for a frame."""
    if frame is None:
        return ""
    path = frame.fullname or frame.file
    if path:
        name = os.path.basename(path)
        return f"{name}:{frame.line}" if frame.line > 0 else name
    if frame.addr:
        return frame.addr
    return ""
