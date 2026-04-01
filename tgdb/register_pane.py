"""
Register pane widget.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .gdb_controller import RegisterInfo
from .highlight_groups import HighlightGroups
from .pane_utils import center_cells, fit_cells


class RegisterPane(Widget):
    """Render register names and values for the current frame."""

    DEFAULT_CSS = """
    RegisterPane {
        width: 1fr;
        height: 1fr;
        min-width: 4;
        min-height: 2;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = True
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

        result.append(center_cells("Registers", width), style=self.hl.style("StatusLine"))

        visible_rows = max(0, height - 1)
        for register in self._registers[:visible_rows]:
            result.append("\n")
            result.append(
                fit_cells(self._register_text(register), width),
                style=self.hl.style("Normal"),
            )

        remaining_rows = height - 1 - min(visible_rows, len(self._registers))
        for _ in range(max(0, remaining_rows)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))

        return result
