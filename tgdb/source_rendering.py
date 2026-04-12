"""Rendering mixin for SourceView — render(), _build_line(), etc."""

from __future__ import annotations

import re
from typing import Optional, TYPE_CHECKING

from rich.cells import cell_len, split_graphemes
from rich.text import Text

if TYPE_CHECKING:
    from .source_data import SourceFile

from .source_data import BP_NONE, BP_ENABLED, BP_DISABLED, _LOGO_LINES


class SourceViewRendering:
    """Mixin that provides all rendering methods for SourceView.

    Expects the host class to supply: hl, source_file, exe_line, sel_line,
    _scroll_top, _col_offset, _show_logo, _global_marks, _search_pattern,
    _search_forward, hlsearch, ignorecase, showmarks, color, tabstop,
    executing_line_display, selected_line_display, size, and
    _visible_height().
    """

    # ------------------------------------------------------------------
    # Rendering — render() only, no render_line() override
    # ------------------------------------------------------------------

    def render(self) -> Text:
        h = self._visible_height()
        w = max(10, self.size.width or 80)
        sf = self.source_file
        result = Text(no_wrap=True, overflow="crop")

        # ── Logo: shown when no file loaded or :logo was called ──
        if sf is None or self._show_logo:
            logo = _LOGO_LINES
            v_pad = max(0, (h - len(logo)) // 2)  # rows above logo
            style = self.hl.style("Logo")
            for y in range(h):
                if y > 0:
                    result.append("\n")
                logo_idx = y - v_pad
                if 0 <= logo_idx < len(logo):
                    line = logo[logo_idx]
                    # horizontal center: pad left with spaces
                    # Use visual width (logo uses wide box chars — count as 1 col each)
                    lw = len(line)
                    left = max(0, (w - lw) // 2)
                    result.append(" " * left + line, style=style)
                # else: empty row (blank padding above/below logo)
            return result

        render_top = self._scroll_top
        n = len(sf.lines)

        if h != getattr(self, "_last_render_h", h):
            # Pane height changed (drag-resize or add/remove pane) —
            # re-centre sel_line using the new height.
            idx = self.sel_line - 1
            if n >= h:
                render_top = max(0, min(idx - h // 2, n - h))
            else:
                render_top = 0
            self._scroll_top = render_top
        elif n >= h:
            # Clamp to prevent blank ~-lines at bottom when pane grew.
            render_top = max(0, min(render_top, n - h))
            self._scroll_top = render_top

        self._last_render_h = h

        if n < h:
            render_top = (n - h) // 2  # vertically centre small files

        for y in range(h):
            line_idx = render_top + y  # 0-based; may be negative for vertical centering
            result.append_text(self._build_line(line_idx, sf))
            if y < h - 1:
                result.append("\n")
        return result

    # Width of the line-number field (minimum 1, grows with file size)
    def _nr_width(self) -> int:
        if self.source_file:
            n = len(self.source_file.lines)
        else:
            n = 0
        return max(1, len(str(max(n, 1))))

    def _get_line_number_style(self, bp_flag: int, is_exe: bool, is_sel: bool) -> str:
        """Return the style for a line number cell.

        Priority order matches cgdb: breakpoint > exe > sel > normal.
        """
        if bp_flag == BP_ENABLED:
            return self.hl.style("Breakpoint")
        if bp_flag == BP_DISABLED:
            return self.hl.style("DisabledBreakpoint")
        if is_exe:
            return self.hl.style("ExecutingLineNr")
        if is_sel:
            return self.hl.style("SelectedLineNr")
        return self.hl.style("LineNumber")

    def _find_mark_for_line(self, sf, line_no: int) -> tuple[str | None, str]:
        """Return ``(mark_char, mark_style)`` for *line_no*, or ``(None, "")``."""
        if not self.showmarks:
            return None, ""
        for mk, ml in sf.marks_local.items():
            if ml == line_no:
                return mk, self.hl.style("Mark")
        for mk, (mp, ml) in self._global_marks.items():
            if mp == sf.path and ml == line_no:
                return mk, self.hl.style("Mark")
        return None, ""

    def _get_arrow_info(self, is_exe: bool, is_sel: bool, exe_disp: str, sel_disp: str) -> tuple[str, str]:
        """Return ``(arrow_style, display_mode)`` for the current line.

        *display_mode* is ``"shortarrow"`` / ``"longarrow"`` / ``""``.
        """
        if is_exe and exe_disp in ("shortarrow", "longarrow"):
            return self.hl.style("ExecutingLineArrow"), exe_disp
        if is_sel and sel_disp in ("shortarrow", "longarrow"):
            return self.hl.style("SelectedLineArrow"), sel_disp
        return "", ""

    def _get_line_background_style(self, is_exe: bool, is_sel: bool, exe_disp: str, sel_disp: str) -> str:
        """Return the background style for a highlighted source line, or ``""``."""
        if is_exe:
            if exe_disp == "highlight":
                return self.hl.style("ExecutingLineHighlight")
            if exe_disp == "block":
                return self.hl.style("ExecutingLineBlock")
        elif is_sel:
            if sel_disp == "highlight":
                return self.hl.style("SelectedLineHighlight")
            if sel_disp == "block":
                return self.hl.style("SelectedLineBlock")
        return ""

    def _compile_search_pattern(self) -> "re.Pattern | None":
        """Compile the current search pattern, respecting *ignorecase*.

        Returns ``None`` when no pattern is set or the pattern is invalid.
        """
        if not (self.hlsearch and self._search_pattern):
            return None
        try:
            flags = re.IGNORECASE if self.ignorecase else 0
            return re.compile(self._search_pattern, flags)
        except re.error:
            return None

    def _build_line(self, line_idx: int, sf: Optional[SourceFile]) -> Text:
        """Build one visible line as Rich Text, matching cgdb's layout.

        Renders line number, gutter character, arrow (if any), syntax-
        highlighted source text, hlsearch overlay, and selection background
        padding — all clipped to the pane width.  Called only when *sf* is
        not ``None`` (the logo case is handled in ``render()``).
        """
        nr_w = self._nr_width()

        # Beyond end of file → vim-style "~│" matching cgdb
        if line_idx < 0 or line_idx >= len(sf.lines):
            filler = Text(no_wrap=True, overflow="crop")
            filler.append(" " * (nr_w - 1), style=self.hl.style("LineNumber"))
            filler.append("~│", style=self.hl.style("LineNumber"))
            return filler

        line_no = line_idx + 1
        is_exe = line_no == self.exe_line
        is_sel = line_no == self.sel_line
        bp_flag = sf.bp_flags[line_idx] if line_idx < len(sf.bp_flags) else BP_NONE
        exe_disp = self.executing_line_display
        sel_disp = self.selected_line_display

        nr_style = self._get_line_number_style(bp_flag, is_exe, is_sel)
        mark_ch, mark_style = self._find_mark_for_line(sf, line_no)
        arrow_style, arrow_disp = self._get_arrow_info(is_exe, is_sel, exe_disp, sel_disp)
        line_bg = self._get_line_background_style(is_exe, is_sel, exe_disp, sel_disp)

        # Compute leading-whitespace gap used only by the long-arrow display
        # mode.  cgdb name: column_offset = leading_ws - (sel_col + 1) where
        # sel_col is the horizontal scroll position (_col_offset).
        arrow_indent = 0
        if arrow_disp == "longarrow":
            src_line = sf.lines[line_idx] if line_idx < len(sf.lines) else ""
            leading_ws = len(src_line) - len(src_line.lstrip())
            arrow_indent = max(0, leading_ws - 1 - self._col_offset)

        # ── Gutter: line number + separator + optional arrow ──────────────
        out = Text(no_wrap=True, overflow="crop")
        out.append(f"{line_no:{nr_w}d}", style=nr_style)
        # Separator: ├ for arrow lines, mark char if set, │ otherwise
        is_arrow_line = bool(arrow_disp)
        if is_arrow_line:
            out.append("├", style=arrow_style)
        elif mark_ch:
            out.append(mark_ch, style=mark_style)
        else:
            out.append("│")
        # Arrow body (after separator)
        if arrow_disp == "shortarrow":
            out.append(">", style=arrow_style)
        elif arrow_disp == "longarrow":
            out.append("─" * arrow_indent + ">", style=arrow_style)
        else:
            out.append(" ")  # normal line: space after │

        # ── Source text ───────────────────────────────────────────────────
        tokens = sf.tokenize(self.tabstop)
        spans = tokens[line_idx] if line_idx < len(tokens) else []
        if not spans:
            spans = [(sf.lines[line_idx], "Normal")]

        # For long arrow: skip arrow_indent leading whitespace cells.
        # For horizontal scroll: skip _col_offset additional display cells.
        total_skip = arrow_indent + self._col_offset
        source_width = max(0, (self.size.width or 80) - cell_len(out.plain))
        styled_spans: list[tuple[str, str]] = []
        for tok_text, group in spans:
            if line_bg:
                st = line_bg
            elif self.color:
                st = self.hl.style(group)
            else:
                # :set color off — preserve bold/reverse/underline attrs only
                # (matches cgdb: uses A_BOLD/A_REVERSE only when color disabled)
                hs = self.hl.get(group)
                attrs = [a for a, on in
                         [("bold", hs.bold), ("reverse", hs.reverse),
                          ("underline", hs.underline)] if on]
                st = " ".join(attrs)
            styled_spans.append((tok_text, st))

        src_t = self._clip_spans_to_cells(styled_spans, total_skip, source_width)

        # hlsearch overlay
        rx = self._compile_search_pattern()
        if rx is not None:
            for m in rx.finditer(src_t.plain):
                src_t.stylize(self.hl.style("Search"), m.start(), m.end())

        out.append_text(src_t)
        # Extend the selection/execution background to the full pane width.
        if line_bg:
            full_w = max(1, self.size.width or 80)
            used = cell_len(out.plain)
            if used < full_w:
                out.append(" " * (full_w - used), style=line_bg)
        # Clip to pane width — source rows must not soft-wrap (matches cgdb).
        out.truncate(max(1, self.size.width or 80), overflow="crop")
        return out


    def _clip_spans_to_cells(self, spans: list[tuple[str, str]], start_cell: int, max_cells: int) -> Text:
        """Clip styled text by display cells.

        If clipping starts or ends in the middle of a wide character, render the
        visible half as '?' so cell-based alignment is preserved during
        horizontal scrolling and truncation.
        """
        out = Text(no_wrap=True, overflow="crop")
        if max_cells <= 0:
            return out

        view_start = max(0, start_cell)
        view_end = view_start + max_cells
        cell_pos = 0

        for tok_text, style in spans:
            graphemes, _ = split_graphemes(tok_text)
            for start, end, width in graphemes:
                grapheme = tok_text[start:end]
                grapheme_start = cell_pos
                grapheme_end = grapheme_start + width
                cell_pos = grapheme_end

                if width <= 0:
                    continue
                if grapheme_end <= view_start:
                    continue
                if grapheme_start >= view_end:
                    return out

                overlap_start = max(grapheme_start, view_start)
                overlap_end = min(grapheme_end, view_end)
                overlap = overlap_end - overlap_start
                if overlap <= 0:
                    continue

                if overlap == width:
                    out.append(grapheme, style=style)
                else:
                    out.append("?" * overlap, style=style)

        return out
