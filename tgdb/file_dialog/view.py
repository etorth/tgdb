"""Geometry, navigation, and rendering helpers for the file dialog widget."""

import math

from rich.text import Text


class FileDialogViewMixin:
    """Mixin providing geometry and rendering for ``FileDialog``."""

    def _h(self) -> int:
        return max(3, self.size.height)


    def _w(self) -> int:
        return max(10, self.size.width)


    def _list_height(self) -> int:
        return max(1, self._h() - 2)


    def _lwidth(self) -> int:
        file_count = len(self._files)
        if file_count == 0:
            return 1
        return int(math.log10(file_count)) + 1


    def _scroll_start(self) -> int:
        file_count = len(self._files)
        height = self._list_height()
        if file_count == 0:
            return 0
        if file_count < height:
            return (file_count - height) // 2
        start = self._sel - height // 2
        return max(0, min(file_count - height, start))


    def _clamp(self, line: int) -> int:
        return max(0, min(len(self._files) - 1, line))


    def _move(self, delta: int) -> None:
        if not self._files:
            return
        self._sel = self._clamp(self._sel + delta)
        self.refresh()


    def _scroll_h(self, delta: int) -> None:
        max_width = max((len(path) for path in self._files), default=0)
        self._sel_col = max(0, min(max_width, self._sel_col + delta))
        self.refresh()


    def render(self) -> Text:
        width = self._w()
        list_height = self._list_height()
        line_width = self._lwidth()
        file_count = len(self._files)
        result = Text(no_wrap=True, overflow="crop")

        label = self._LABEL
        pad = max(0, (width - len(label)) // 2)
        result.append(" " * pad + label, style=self.hl.style("CommandLine"))
        result.append(
            " " * max(0, width - pad - len(label)),
            style=self.hl.style("CommandLine"),
        )
        result.append("\n")

        if self._query_pending or (file_count == 0 and self._body_message):
            message = self._body_message or ""
            message_row = max(0, (list_height - 1) // 2)
            for row_index in range(list_height):
                if row_index:
                    result.append("\n")
                if row_index == message_row and message:
                    pad = max(0, (width - len(message)) // 2)
                    result.append(" " * pad, style=self.hl.style("Normal"))
                    result.append(message, style=self.hl.style("CommandLine"))
                    result.append(
                        " " * max(0, width - pad - len(message)),
                        style=self.hl.style("Normal"),
                    )
                else:
                    result.append(" " * width, style=self.hl.style("Normal"))

            result.append("\n")
            bar = " Esc=quit" if self._query_pending else " q=quit"
            result.append(bar[:width].ljust(width), style=self.hl.style("CommandLine"))
            return result

        start = self._scroll_start()
        for row_index in range(1, list_height + 1):
            file_index = start + (row_index - 1)
            if row_index > 1:
                result.append("\n")

            if file_index < 0 or file_index >= file_count:
                result.append(" " * line_width, style=self.hl.style("LineNumber"))
                result.append("~", style=self.hl.style("LineNumber"))
                result.append("│", style="bold")
                continue

            filename = self._files[file_index]
            if self._sel_col < len(filename):
                display_name = filename[self._sel_col :]
            else:
                display_name = ""

            if file_index == self._sel:
                result.append(
                    f"{file_index + 1:{line_width}d}",
                    style="bold " + self.hl.style("SelectedLineNr"),
                )
                result.append("->", style="bold " + self.hl.style("SelectedLineArrow"))
                result.append(display_name, style=self.hl.style("SelectedLineHighlight"))
            else:
                result.append(
                    f"{file_index + 1:{line_width}d}",
                    style=self.hl.style("LineNumber"),
                )
                result.append("│", style="bold")
                result.append(" " + display_name)

        result.append("\n")
        if self._search_active:
            prefix = "/" if self._search_forward else "?"
            bar = f"{prefix}{self._search_buf}"
        else:
            current = self._sel + 1 if self._sel >= 0 else 0
            bar = f" {current}/{file_count}  j/k=move  Enter=open  /=search  q=quit"
        result.append(bar[:width].ljust(width), style=self.hl.style("CommandLine"))
        return result
