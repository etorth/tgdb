"""
Watch pane — user-defined expression evaluator.
Expressions are evaluated via GDB -data-evaluate-expression on each stop.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from rich.text import Text
from textual.widget import Widget

from .highlight_groups import HighlightGroups
from .pane_base import PaneBase
from .pane_utils import fit_cells


class _WatchContent(Widget):
    """Renders the watch expression list (no title row)."""

    DEFAULT_CSS = """
    _WatchContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._entries: list[tuple[str, str]] = []

    def set_entries(self, entries: list[tuple[str, str]]) -> None:
        self._entries = list(entries)
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")
        for i, (expr, value) in enumerate(self._entries[:height]):
            if i > 0:
                result.append("\n")
            line = f"{i + 1}: {expr} = {value}"
            result.append(fit_cells(line, width), style=self.hl.style("Normal"))
        remaining = height - min(height, len(self._entries))
        for i in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


class WatchPane(PaneBase):
    """Watch pane: title bar + expression/value list."""

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _WatchContent(hl)
        self._expressions: list[str] = []
        self._values: list[str] = []
        self._eval_fn: Optional[Callable] = None

    def title(self) -> str:
        return "WATCH"

    def compose(self):
        yield from super().compose()
        yield self._content

    def set_eval_fn(self, fn: Callable) -> None:
        self._eval_fn = fn

    def _update_content(self) -> None:
        self._content.set_entries(list(zip(self._expressions, self._values)))

    def add_expression(self, expr: str) -> None:
        idx = len(self._expressions)
        self._expressions.append(expr)
        self._values.append("<pending>")
        self._update_content()
        asyncio.create_task(self._eval_one(idx, expr))

    def remove_expression(self, index: int) -> Optional[str]:
        if 0 <= index < len(self._expressions):
            removed = self._expressions.pop(index)
            self._values.pop(index)
            self._update_content()
            return removed
        return None

    async def _eval_one(self, idx: int, expr: str) -> None:
        if self._eval_fn:
            try:
                val = await self._eval_fn(expr)
            except Exception:
                val = "<error>"
            if idx < len(self._values):
                self._values[idx] = val
                self._update_content()

    async def refresh_all(self, current_frame: Optional[object] = None) -> None:
        for i, expr in enumerate(self._expressions):
            if self._eval_fn:
                try:
                    val = await self._eval_fn(expr)
                except Exception:
                    val = "<error>"
                if i < len(self._values):
                    self._values[i] = val
        self._update_content()
