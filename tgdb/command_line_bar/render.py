"""Rendering mixin for CommandLineBar."""

from __future__ import annotations

from rich.text import Text


def _pad_crop(text: str, w: int) -> str:
    """Return *text* truncated or space-padded to exactly *w* characters."""
    if len(text) >= w:
        return text[:w]
    return text + " " * (w - len(text))


class RenderMixin:
    """Mixin providing all render methods for CommandLineBar.

    All instance attributes referenced here are initialised in
    ``CommandLineBar.__init__``.
    """

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w = max(10, self.size.width or 80)
        style = self.hl.style("CommandLine")

        if self._task_running:
            return self._render_streaming(w, style)

        if self._ml_active:
            return self._render_ml_input(w, style)

        if self._msg_lines:
            return self._render_msg(w, style)

        # ── Single-line modes ─────────────────────────────────────────
        if self._input_active:
            if self._popup_active:
                return self._render_input_with_popup(w, style)
            return self._render_input(w, style)

        if self._search_active:
            if self._search_forward:
                pfx = "/"
            else:
                pfx = "?"
            t = Text(
                _pad_crop(f"{pfx}{self._search_buf}", w), no_wrap=True, overflow="crop"
            )
            t.stylize(style)
            return t

        # Fire-and-forget async-print-op (task finished but background coroutine printed)
        if self._streaming_buf:
            return self._render_streaming(w, style)

        if self._message:
            t = Text(
                _pad_crop(self._message, w), style=style, no_wrap=True, overflow="crop"
            )
            return t

        return Text(" " * w, style=style, no_wrap=True, overflow="crop")


    def _render_input(self, w: int, style: str) -> Text:
        """Render the active command-input line with a blinking block cursor."""
        prefix = ":"
        buf = self._input_buf
        cursor = max(0, min(self._cursor_pos, len(buf)))

        full = prefix + buf
        cursor_in_full = len(prefix) + cursor

        # Scroll the view so the cursor is always visible
        if cursor_in_full >= w:
            start = cursor_in_full - w + 1
        else:
            start = 0

        # Visible slice, padded to exactly w chars
        visible = full[start:]
        if len(visible) < w:
            visible += " " * (w - len(visible))
        else:
            visible = visible[:w]

        cursor_col = cursor_in_full - start  # column of cursor in visible slice

        # Build cursor style: toggle 'reverse' so the cursor always contrasts
        # with the surrounding text. If the bar style already uses 'reverse'
        # (the default), removing it from the cursor makes it stand out as a
        # "normal" cell against the reversed background.  If the bar style does
        # not use reverse, adding it creates the same contrast.
        style_tokens = style.lower().split()
        if "reverse" in style_tokens:
            other_tokens = []
            for t in style_tokens:
                if t != "reverse":
                    other_tokens.append(t)
            if other_tokens:
                cursor_style = " ".join(other_tokens)
            else:
                cursor_style = "default"
        else:
            if style and style != "default":
                cursor_style = f"reverse {style}"
            else:
                cursor_style = "reverse"

        t = Text(no_wrap=True, overflow="crop")
        if cursor_col > 0:
            t.append(visible[:cursor_col], style)
        if cursor_col < len(visible):
            ch = visible[cursor_col]
        else:
            ch = " "
        t.append(ch, cursor_style)
        after_col = cursor_col + 1
        if after_col < len(visible):
            t.append(visible[after_col:], style)
        return t


    def _render_streaming(self, w: int, style: str) -> Text:
        """Render async-print-op: show latest print output with ▶ prefix.

        First line is prefixed with ``▶ ``; continuation lines are indented
        with three spaces to align with the text after ``▶ ``.
        """
        buf = self._streaming_buf
        if buf:
            lines = buf.rstrip("\n").split("\n")
            rendered = []
            for i, ln in enumerate(lines):
                if i == 0:
                    rendered.append(_pad_crop(f"\u25b6 {ln}", w))
                else:
                    rendered.append(_pad_crop(f"   {ln}", w))
            t = Text("\n".join(rendered))
            t.stylize(style)
            return t
        text = _pad_crop("\u25b6 Running\u2026", w)
        return Text(text, style=style, no_wrap=True, overflow="crop")


    def _render_ml_input(self, w: int, style: str) -> Text:
        """Render the heredoc continuation prompt (multi-row)."""
        lines = []
        if self._ml_history_recall:
            # History-recalled heredoc — show verbatim lines with ':' prefix on first
            for i, ln in enumerate(self._ml_buf):
                if i == 0:
                    lines.append(_pad_crop(f":{ln}", w))
                else:
                    lines.append(_pad_crop(f" {ln}", w))
        else:
            lines.append(_pad_crop(f":{self._ml_header}", w))
            for ln in self._ml_buf:
                lines.append(_pad_crop(f"  {ln}", w))
            # Current input row — append a block cursor marker
            lines.append(_pad_crop(f"  {self._input_buf}\u258f", w))
        t = Text("\n".join(lines))
        t.stylize(style)
        return t


    def _render_msg(self, w: int, style: str) -> Text:
        """Render the visible window of the scrollable message display."""
        visible = max(1, self._msg_visible_rows)
        window = self._msg_lines[self._msg_scroll : self._msg_scroll + visible]
        lines = []
        for ln in window:
            lines.append(_pad_crop(ln, w))

        # Pad blank rows if the window is taller than remaining content
        while len(lines) < visible:
            lines.append(" " * w)

        at_end = self._msg_scroll + visible >= len(self._msg_lines)
        if at_end:
            hint = "-- Press any key to continue --"
        else:
            hint = "-- Use j/k to scroll more lines --"
        lines.append(_pad_crop(hint, w))

        t = Text("\n".join(lines))
        t.stylize(style)
        return t


    def _render_input_with_popup(self, w: int, style: str) -> Text:
        """Render the input row with a wildmenu-style completion popup above it.

        The popup occupies the rows above the input line.  When more
        candidates exist than fit in ``self._popup_max_rows`` only a sliding
        window covering the current selection is shown.
        """
        items = self._completions
        if not items:
            return self._render_input(w, style)

        max_item_w = max(len(item) for item in items)
        popup_w = min(max_item_w + 2, w)
        rows = max(1, min(self._popup_max_rows, len(items)))
        scroll = max(0, min(self._popup_scroll, len(items) - rows))

        item_style = self.hl.style("Pmenu")
        sel_style = self.hl.style("PmenuSel")

        t = Text(no_wrap=True, overflow="crop")
        for row in range(rows):
            idx = scroll + row
            item = items[idx]
            cell = " " + item
            if len(cell) < popup_w:
                cell += " " * (popup_w - len(cell))
            else:
                cell = cell[:popup_w]
            if idx == self._completion_idx:
                t.append(cell, sel_style)
            else:
                t.append(cell, item_style)
            remaining = w - popup_w
            if remaining > 0:
                t.append(" " * remaining, style)
            t.append("\n")

        t.append_text(self._render_input(w, style))
        return t
