"""
Public implementation of the disassembly-pane package.

``DisasmPane`` is a black-box disassembly viewer. The caller constructs the
widget, injects one async disassembly callback, and either pushes parsed lines
directly with ``set_disasm(...)`` or asks the pane to refresh itself from a
source location with ``refresh_disasm(...)``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from rich.text import Text
from textual import events
from textual.widget import Widget

from ..highlight_groups import HighlightGroups
from ..pane_chrome import PaneBase
from ..pane_utils import fit_cells


@dataclass
class DisasmLine:
    addr: str = ""
    func_name: str = ""
    offset: int = 0
    inst: str = ""
    src_file: str = ""
    src_line: int = 0
    is_current: bool = False


class _DisasmContent(Widget):
    """Renders disassembly lines with navigation (no title row)."""

    DEFAULT_CSS = """
    _DisasmContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = True
        self._lines: list[DisasmLine] = []
        self._current_addr: str = ""
        self._scroll_top: int = 0
        self._selected: int = 0
        self._await_g: bool = False


    def set_disasm(self, lines: list[DisasmLine], current_addr: str = "") -> None:
        self._lines = list(lines)
        self._current_addr = current_addr
        # Scroll to current PC
        for i, line in enumerate(self._lines):
            if line.addr == current_addr or line.is_current:
                self._selected = i
                break
        self._ensure_visible()
        self.refresh()


    def _ensure_visible(self) -> None:
        height = max(1, self.size.height or 1)
        if self._selected < self._scroll_top:
            self._scroll_top = self._selected
        elif self._selected >= self._scroll_top + height:
            self._scroll_top = self._selected - height + 1


    def _format_line(self, line: DisasmLine) -> str:
        func_part = f"<{line.func_name}+{line.offset}>" if line.func_name else ""
        parts = [line.addr]
        if func_part:
            parts.append(func_part)
        parts.append(line.inst)
        return "  ".join(p for p in parts if p)


    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")
        visible = self._lines[self._scroll_top:self._scroll_top + height]
        for i, line in enumerate(visible):
            if i > 0:
                result.append("\n")
            is_pc = line.addr == self._current_addr or line.is_current
            style = self.hl.style("ExecutingLineBlock") if is_pc else self.hl.style("Normal")
            result.append(fit_cells(self._format_line(line), width), style=style)
        remaining = height - len(visible)
        for _ in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("j", "down"):
            self._selected = min(len(self._lines) - 1, self._selected + 1) if self._lines else 0
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
            self._selected = max(0, len(self._lines) - 1)
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
        else:
            self._await_g = False


class DisasmPane(PaneBase):
    """Render the current function's disassembly.

    Public interface
    ----------------
    ``DisasmPane(hl, **kwargs)``
        Create the widget.

    ``set_disasm(lines, current_addr="")``
        Replace the visible disassembly with already-parsed lines.

    ``set_disasm_fn(fn)``
        Inject the async callback that asks GDB for disassembly data.

    ``refresh_disasm(filename, line, current_addr="")``
        Query GDB for disassembly near the given source location and redraw the
        pane when the result arrives.
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        """Create an empty disassembly pane."""
        super().__init__(hl, **kwargs)
        self._content = _DisasmContent(hl)
        self._disasm_fn: Optional[Callable] = None


    def title(self) -> str:
        return "DISASM"


    def compose(self):
        yield from super().compose()
        yield self._content


    def set_disasm(self, lines: list[DisasmLine], current_addr: str = "") -> None:
        """Publish a parsed disassembly snapshot."""
        self._content.set_disasm(lines, current_addr)


    def set_disasm_fn(self, fn: Callable) -> None:
        """Install the async callback used to request disassembly from GDB."""
        self._disasm_fn = fn


    async def refresh_disasm(self, filename: str, line: int, current_addr: str = "") -> None:
        """Fetch and display disassembly near a source location."""
        if not self._disasm_fn:
            return
        try:
            raw = await self._disasm_fn(filename, line)
        except Exception:
            return
        lines = _parse_disasm(raw, current_addr)
        self.set_disasm(lines, current_addr)


def _parse_disasm(raw: list[dict], current_addr: str = "") -> list[DisasmLine]:
    """Parse MI -data-disassemble result into DisasmLine list.

    Handles both mode=0 (flat insn list) and mode=1 (src_and_asm_line dicts).
    """
    result: list[DisasmLine] = []

    def _make_line(insn: dict, src_file: str = "", src_line: int = 0) -> DisasmLine:
        addr = insn.get("address", "")
        try:
            offset = int(insn.get("offset", 0))
        except (ValueError, TypeError):
            offset = 0
        dl = DisasmLine(
            addr=addr,
            func_name=insn.get("func-name", ""),
            offset=offset,
            inst=insn.get("inst", ""),
            src_file=src_file,
            src_line=src_line,
            is_current=(addr == current_addr),
        )
        return dl

    if not raw:
        return result

    first = raw[0] if raw else {}
    # mode=1: each element has line_asm_insn key
    if "line_asm_insn" in first:
        for src_block in raw:
            src_file = src_block.get("file", src_block.get("fullname", ""))
            try:
                src_line_num = int(src_block.get("line", 0))
            except (ValueError, TypeError):
                src_line_num = 0
            for insn in src_block.get("line_asm_insn", []):
                result.append(_make_line(insn, src_file, src_line_num))
    else:
        # mode=0: flat list of insn dicts
        for insn in raw:
            result.append(_make_line(insn))

    return result
