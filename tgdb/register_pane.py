"""
Register pane widget.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .gdb_controller import RegisterInfo
from .highlight_groups import HighlightGroups
from .pane_base import PaneBase
from .pane_utils import fit_cells


class _RegisterContent(Widget):
    """Renders the register list (no title row)."""

    DEFAULT_CSS = """
    _RegisterContent {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = False
        self._registers: list[RegisterInfo] = []

    def set_registers(self, registers: list[RegisterInfo]) -> None:
        self._registers = list(registers)
        self.refresh()

    def _register_text(self, register: RegisterInfo) -> str:
        return f"{register.name} = {register.value}"

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")
        for i, register in enumerate(self._registers[:height]):
            if i > 0:
                result.append("\n")
            result.append(fit_cells(self._register_text(register), width), style=self.hl.style("Normal"))
        remaining = height - min(height, len(self._registers))
        for i in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


class RegisterPane(PaneBase):
    """Register pane: title bar + register list."""

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._content = _RegisterContent(hl)

    def title(self) -> str:
        return "Registers"

    def compose(self):
        yield from super().compose()
        yield self._content

    def set_registers(self, registers: list[RegisterInfo]) -> None:
        self._content.set_registers(registers)
