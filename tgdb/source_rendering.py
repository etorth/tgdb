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
            if h > 0:
                result.append("\n")
            result.append_text(self._build_footer(w, None))
            return result

        render_top = self._scroll_top
        if len(sf.lines) < h:
            render_top = (len(sf.lines) - h) // 2

        for y in range(h):
            line_idx = render_top + y   # 0-based; may be negative for vertical centering
            result.append_text(self._build_line(line_idx, sf))
            if y < h - 1:
                result.append("\n")
        if h > 0:
            result.append("\n")
        result.append_text(self._build_footer(w, sf))
        return result

    # Width of the line-number field (minimum 1, grows with file size)
    def _nr_width(self) -> int:
        n = len(self.source_file.lines) if self.source_file else 0
        return max(1, len(str(max(n, 1))))

    def _build_line(self, line_idx: int, sf: Optional[SourceFile]) -> Text:
        """Build one visible line as Rich Text, matching cgdb's layout.
        Called only when sf is not None (logo is handled in render())."""
        nr_w = self._nr_width()

        # Beyond end of file → vim-style "~│" matching cgdb: (lwidth-1 spaces)(~)(│)
        if line_idx < 0 or line_idx >= len(sf.lines):
            filler = Text(no_wrap=True, overflow="crop")
            filler.append(" " * (nr_w - 1), style=self.hl.style("LineNumber"))
            filler.append("~│", style=self.hl.style("LineNumber"))
            return filler

        line_no = line_idx + 1
        is_exe = (line_no == self.exe_line)
        is_sel = (line_no == self.sel_line)
        bp_flag = sf.bp_flags[line_idx] if line_idx < len(sf.bp_flags) else BP_NONE
        exe_disp = self.executing_line_display
        sel_disp = self.selected_line_display

        # ── Line number style (cgdb priority: breakpoint > exe > sel > normal) ──
        if bp_flag == BP_ENABLED:
            nr_style = self.hl.style("Breakpoint")
        elif bp_flag == BP_DISABLED:
            nr_style = self.hl.style("DisabledBreakpoint")
        elif is_exe:
            nr_style = self.hl.style("ExecutingLineNr")
        elif is_sel:
            nr_style = self.hl.style("SelectedLineNr")
        else:
            nr_style = self.hl.style("LineNumber")

        # ── Mark: replaces │ with mark char (cgdb: vert_bar_char = mark char) ──
        mark_ch = None
        mark_st = ""
        if self.showmarks:
            for mk, ml in sf.marks_local.items():
                if ml == line_no:
                    mark_ch = mk
                    mark_st = self.hl.style("Mark")
                    break
            if mark_ch is None:
                for mk, (mp, ml) in self._global_marks.items():
                    if mp == sf.path and ml == line_no:
                        mark_ch = mk
                        mark_st = self.hl.style("Mark")
                        break

        # ── Separator and arrow — matches cgdb sources.cpp ──
        # cgdb: vert_bar = SWIN_SYM_LTEE (├) for arrow lines, SWIN_SYM_VLINE (│) otherwise
        # After vert_bar: space for normal; '>' for short arrow;
        #   '─'×col_off + '>' for long arrow, then source from col_off.
        is_arrow_line = (
            (is_exe and exe_disp in ("shortarrow", "longarrow")) or
            (is_sel and sel_disp in ("shortarrow", "longarrow"))
        )
        if is_exe and exe_disp in ("shortarrow", "longarrow"):
            arrow_st = self.hl.style("ExecutingLineArrow")
            disp = exe_disp
        elif is_sel and sel_disp in ("shortarrow", "longarrow"):
            arrow_st = self.hl.style("SelectedLineArrow")
            disp = sel_disp
        else:
            arrow_st = ""
            disp = ""

        # Compute long-arrow column_offset (cgdb: leading_ws - (sel_col+1))
        # sel_col is _col_offset (horizontal scroll position)
        col_off = 0
        if disp == "longarrow":
            src_line = sf.lines[line_idx] if line_idx < len(sf.lines) else ""
            leading_ws = len(src_line) - len(src_line.lstrip())
            col_off = max(0, leading_ws - 1 - self._col_offset)

        out = Text(no_wrap=True, overflow="crop")
        # Right-aligned line number — no separate gutter column (matches cgdb)
        out.append(f"{line_no:{nr_w}d}", style=nr_style)
        # vert_bar: ├ for arrow lines, mark char if set, │ otherwise
        if is_arrow_line:
            out.append("├", style=arrow_st)
        elif mark_ch:
            out.append(mark_ch, style=mark_st)
        else:
            out.append("│")
        # Arrow body (after vert_bar)
        if disp == "shortarrow":
            out.append(">", style=arrow_st)
        elif disp == "longarrow":
            out.append("─" * col_off + ">", style=arrow_st)
        else:
            out.append(" ")  # normal line: space after │ (cgdb behaviour)

        # ── Source text ──
        if is_exe:
            if exe_disp == "highlight":
                line_bg = self.hl.style("ExecutingLineHighlight")
            elif exe_disp == "block":
                line_bg = self.hl.style("ExecutingLineBlock")
            else:
                line_bg = ""
        elif is_sel:
            if sel_disp == "highlight":
                line_bg = self.hl.style("SelectedLineHighlight")
            elif sel_disp == "block":
                line_bg = self.hl.style("SelectedLineBlock")
            else:
                line_bg = ""
        else:
            line_bg = ""

        tokens = sf.tokenize(self.tabstop)
        spans = tokens[line_idx] if line_idx < len(tokens) else []
        if not spans:
            spans = [(sf.lines[line_idx], "Normal")]

        # For long arrow: skip col_off leading whitespace cells (cgdb: sel_col + column_offset)
        # For horizontal scroll (_col_offset): skip additional display cells from the source text.
        total_skip = col_off + self._col_offset
        source_width = max(0, (self.size.width or 80) - cell_len(out.plain))
        styled_spans: list[tuple[str, str]] = []
        for tok_text, group in spans:
            if line_bg:
                st = line_bg
            elif self.color:
                st = self.hl.style(group)
            else:
                # :set color off — strip colors, keep only bold/reverse attrs
                # (matches cgdb: uses A_BOLD/A_REVERSE only when color disabled)
                hs = self.hl.get(group)
                attrs = []
                if hs.bold:
                    attrs.append("bold")
                if hs.reverse:
                    attrs.append("reverse")
                if hs.underline:
                    attrs.append("underline")
                st = " ".join(attrs) if attrs else ""
            styled_spans.append((tok_text, st))

        src_t = self._clip_spans_to_cells(styled_spans, total_skip, source_width)

        # hlsearch overlay
        if self.hlsearch and self._search_pattern:
            try:
                flags = re.IGNORECASE if self.ignorecase else 0
                rx = re.compile(self._search_pattern, flags)
                for m in rx.finditer(src_t.plain):
                    src_t.stylize(self.hl.style("Search"), m.start(), m.end())
            except re.error:
                pass

        out.append_text(src_t)
        # Match cgdb: source rows are clipped to the pane width instead of
        # soft-wrapping onto following rows.
        out.truncate(max(1, self.size.width or 80), overflow="crop")
        return out

    def _clip_spans_to_cells(
        self, spans: list[tuple[str, str]], start_cell: int, max_cells: int
    ) -> Text:
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

    def _build_footer(self, width: int, sf: Optional[SourceFile]) -> Text:
        footer = Text(no_wrap=True, overflow="crop")
        path = ""
        if sf is not None and not self._show_logo:
            path = sf.path
            if len(path) > width:
                path = "…" + path[-(width - 1):]
        footer.append(path.ljust(width), style=self.hl.style("StatusLine"))
        return footer
