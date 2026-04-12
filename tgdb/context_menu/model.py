"""Shared data model for the cascading context-menu package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rich.cells import cell_len


@dataclass(frozen=True)
class ContextMenuItem:
    """Immutable menu entry used to build the cascading context menu tree."""

    label: str
    action: Optional[str] = None
    children: tuple["ContextMenuItem", ...] = ()
    separator_before: bool = False

    @property
    def has_children(self) -> bool:
        return bool(self.children)


@dataclass(frozen=True)
class _PanelRow:
    kind: str
    item_index: Optional[int] = None


@dataclass(frozen=True)
class _PanelLayout:
    items: tuple[ContextMenuItem, ...]
    selected_index: int
    x: int
    y: int
    inner_width: int
    rows: tuple[_PanelRow, ...]

    @property
    def width(self) -> int:
        return self.inner_width + 2


    @property
    def height(self) -> int:
        return len(self.rows) + 2


    def row_for_item(self, item_index: int) -> Optional[int]:
        for row_index, row in enumerate(self.rows):
            if row.kind == "item" and row.item_index == item_index:
                return row_index
        return None


_PADDING_LEFT = 2
_PADDING_RIGHT = 2
_SUBMENU_GLYPH = "▸"


def _item_row_text(panel: _PanelLayout, item: ContextMenuItem) -> str:
    left = " " * _PADDING_LEFT
    right = " " * _PADDING_RIGHT
    if item.has_children:
        tail = f" {_SUBMENU_GLYPH} "
        filler = max(
            1,
            panel.inner_width - cell_len(left) - cell_len(item.label) - cell_len(tail),
        )
        return f"{left}{item.label}{' ' * filler}{tail}"

    filler = max(
        0, panel.inner_width - cell_len(left) - cell_len(item.label) - cell_len(right)
    )
    return f"{left}{item.label}{' ' * filler}{right}"
