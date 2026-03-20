"""
Local variables pane widget.
"""
from __future__ import annotations

from rich.cells import cell_len, split_graphemes
from rich.text import Text
from textual.widget import Widget

from .gdb_controller import LocalVariable
from .highlight_groups import HighlightGroups


def _fit_cells(text: str, width: int) -> str:
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


class LocalVariablePane(Widget):
    """Render the current frame's local variables and arguments."""

    DEFAULT_CSS = """
    LocalVariablePane {
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
        self._variables: list[LocalVariable] = []

    def set_variables(self, variables: list[LocalVariable]) -> None:
        self._variables = list(variables)
        self.refresh()

    @staticmethod
    def _display_value(variable: LocalVariable) -> str:
        if variable.value:
            return variable.value.replace("\n", " ")
        return "<complex>"

    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")

        header = _fit_cells("Local Variables", width)
        result.append(header, style=self.hl.style("StatusLine"))

        visible_rows = max(0, height - 1)
        for variable in self._variables[:visible_rows]:
            prefix = "[arg] " if variable.is_arg else "      "
            line = f"{prefix}{variable.name} = {self._display_value(variable)}"
            result.append("\n")
            result.append(_fit_cells(line, width), style=self.hl.style("Normal"))

        remaining_rows = height - 1 - min(visible_rows, len(self._variables))
        for _ in range(max(0, remaining_rows)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))

        return result
