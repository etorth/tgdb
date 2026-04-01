"""
Source view widget — mirrors cgdb's sources.cpp / interface.cpp source pane.

Features:
  • Syntax highlighting via Pygments
  • Vi-like navigation (j/k, G/gg, Ctrl-b/f/u/d, H/M/L)
  • Breakpoint: line number shown in bold red (enabled) / bold yellow (disabled), set with Space
  • Executing line indicator (shortarrow/longarrow/highlight/block)
  • Selected line indicator (same styles)
  • Regex search (/ ? n N) with optional hlsearch
  • Marks (m[a-z/A-Z], '[a-z/A-Z], '', '.)
  • Auto-reload on file change
  • Footer row showing the current file path
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.token import Token
from textual.widget import Widget
from textual import events
from textual.message import Message
from rich.cells import cell_len, split_graphemes
from rich.text import Text
from rich.style import Style

from .highlight_groups import HighlightGroups
from .gdb_controller import Breakpoint

# ---------------------------------------------------------------------------
# Pygments token → highlight group
# ---------------------------------------------------------------------------
_TOKEN_GROUPS: list[tuple] = [
    (Token.Comment.Preproc, "PreProc"),
    (Token.Comment.PreprocFile, "PreProc"),
    (Token.Comment, "Comment"),
    (Token.Keyword.Type, "Type"),
    (Token.Keyword, "Statement"),
    (Token.Name.Builtin, "Statement"),
    (Token.Literal.String, "Constant"),
    (Token.Literal.Number, "Constant"),
    (Token.Literal, "Constant"),
    (Token.Name.Decorator, "PreProc"),
]


def _token_group(ttype) -> str:
    for tok, group in _TOKEN_GROUPS:
        if ttype in tok:
            return group
    return "Normal"


BP_NONE = 0
BP_ENABLED = 1
BP_DISABLED = 2

# ---------------------------------------------------------------------------
# Per-file source data
# ---------------------------------------------------------------------------


class SourceFile:
    def __init__(self, path: str, lines: list[str]) -> None:
        self.path = path
        self.lines = lines
        self.mtime: float = 0.0
        try:
            self.mtime = os.path.getmtime(path)
        except OSError:
            pass
        self.bp_flags: list[int] = [BP_NONE] * len(lines)
        self.marks_local: dict[str, int] = {}   # 'a'-'z' → 1-based line
        self._tokens: Optional[list[list[tuple]]] = None

    def tokenize(self, tabstop: int = 8) -> list[list[tuple]]:
        """Return per-line list of (text, group) spans, cached."""
        if self._tokens is not None:
            return self._tokens
        src = "\n".join(self.lines)
        try:
            lexer = get_lexer_for_filename(self.path, stripnl=False)
        except Exception:
            lexer = TextLexer(stripnl=False)
        tokens_flat = list(lexer.get_tokens(src))
        lines_toks: list[list[tuple]] = []
        current: list[tuple] = []
        for ttype, val in tokens_flat:
            group = _token_group(ttype)
            parts = val.split("\n")
            for i, part in enumerate(parts):
                if part:
                    current.append((part, group))
                if i < len(parts) - 1:
                    lines_toks.append(current)
                    current = []
        if current:
            lines_toks.append(current)
        while len(lines_toks) < len(self.lines):
            lines_toks.append([])
        self._tokens = lines_toks[:len(self.lines)]
        return self._tokens


# Logo shown when no source file is loaded
_LOGO_LINES = [
    "",
    "████████╗ ██████╗ ██████╗ ██████╗ ",
    "   ██╔══╝██╔════╝ ██╔══██╗██╔══██╗",
    "   ██║   ██║  ███╗██║  ██║██████╔╝",
    "   ██║   ██║   ██║██║  ██║██╔══██╗",
    "   ██║   ╚██████╔╝██████╔╝██████╔╝",
    "   ╚═╝    ╚═════╝ ╚═════╝ ╚═════╝ ",
    "",
    "Vi-like TUI front-end for GDB",
    "Compatible with CGDB keybindings, :help for more details",
]


# ---------------------------------------------------------------------------
# Source view widget
# ---------------------------------------------------------------------------

class SourceView(Widget):
    """Scrollable, syntax-highlighted source viewer with vi keybindings."""

    DEFAULT_CSS = """
    SourceView {
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.source_file: Optional[SourceFile] = None
        self.exe_line: int = 0           # 1-based; 0 = none
        self.sel_line: int = 1           # 1-based cursor
        self._scroll_top: int = 0        # 0-based first visible line
        self._search_pattern: str = ""
        self._search_forward: bool = True
        self._search_active: bool = False
        self._search_buf: str = ""
        self.tabstop: int = 8
        self.executing_line_display: str = "longarrow"
        self.selected_line_display: str = "block"
        self.hlsearch: bool = False
        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self.showmarks: bool = True
        self.color: bool = True          # :set color — enables/disables syntax colors
        self._global_marks: dict[str, tuple[str, int]] = {}
        self._last_jump_line: int = 1
        self._num_buf: str = ""
        self._await_g: bool = False      # true after first 'g' (for 'gg')
        self._col_offset: int = 0        # horizontal scroll (cgdb sel_col)
        self._show_logo: bool = False    # force logo display (:logo command)
        self._file_positions: dict[str, int] = {}
        self._pending_search: Optional[tuple[str, bool]] = None
        self.can_focus = True

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def load_file(self, path: str) -> bool:
        try:
            previous = self.source_file
            if previous:
                self._file_positions[previous.path] = self.sel_line

            with open(path, errors="replace") as f:
                content = f.read()
            lines = content.expandtabs(self.tabstop).splitlines()
            if not lines:
                lines = [""]
            sf = SourceFile(path, lines)
            if previous and previous.path == path:
                sf.bp_flags = list(previous.bp_flags[:len(lines)])
                while len(sf.bp_flags) < len(lines):
                    sf.bp_flags.append(BP_NONE)
                sf.marks_local = dict(previous.marks_local)
            self.source_file = sf
            self._show_logo = False
            self._col_offset = 0
            target_line = self._file_positions.get(path, 1)
            self.sel_line = max(1, min(target_line, len(lines)))
            self._ensure_visible(self.sel_line)
            self.refresh()
            return True
        except OSError:
            return False

    def reload_if_changed(self) -> bool:
        sf = self.source_file
        if not sf:
            return False
        try:
            mtime = os.path.getmtime(sf.path)
            if mtime != sf.mtime:
                return self.load_file(sf.path)
        except OSError:
            pass
        return False

    def set_breakpoints(self, bps: list[Breakpoint]) -> None:
        sf = self.source_file
        if not sf:
            return
        sf.bp_flags = [BP_NONE] * len(sf.lines)
        for bp in bps:
            fullname = bp.fullname or bp.file
            if not fullname:
                continue
            try:
                same = (os.path.abspath(fullname) == os.path.abspath(sf.path)
                        or os.path.basename(fullname) == os.path.basename(sf.path))
            except Exception:
                same = False
            if same and 1 <= bp.line <= len(sf.lines):
                sf.bp_flags[bp.line - 1] = (
                    BP_ENABLED if bp.enabled else BP_DISABLED
                )
        self.refresh()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _line_count(self) -> int:
        if self.source_file:
            return len(self.source_file.lines)
        return len(_LOGO_LINES)

    def _visible_height(self) -> int:
        total_height = max(1, self.size.height)
        return max(0, total_height - 1)

    def _ensure_visible(self, line: int) -> None:
        """Mirror cgdb: keep the selected line centered when possible."""
        h = self._visible_height()
        n = self._line_count()
        idx = line - 1
        if n <= 0 or n < h:
            self._scroll_top = 0
        else:
            self._scroll_top = max(0, min(idx - h // 2, n - h))

    def move_to(self, line: int) -> None:
        n = self._line_count()
        self.sel_line = max(1, min(line, n if n else 1))
        self._ensure_visible(self.sel_line)
        self.refresh()

    def scroll_up(self, n: int = 1) -> None: self.move_to(self.sel_line - n)
    def scroll_down(self, n: int = 1) -> None: self.move_to(self.sel_line + n)

    def scroll_col(self, delta: int) -> None:
        """Horizontal scroll — cgdb sel_col."""
        self._col_offset = max(0, self._col_offset + delta)
        self.refresh()

    def scroll_col_to(self, col: int) -> None:
        """Set horizontal scroll to an absolute display-column position.

        Pass col=999999 to scroll to the end of the currently selected line.
        """
        if col >= 999999:
            if self.source_file and 1 <= self.sel_line <= len(self.source_file.lines):
                from rich.cells import cell_len as _cell_len
                line_text = self.source_file.lines[self.sel_line - 1]
                line_cells = _cell_len(line_text)
                # Account for line-number gutter (approx 4–6 cols); use width−6
                visible_w = max(1, (self.size.width or 80) - 6)
                col = max(0, line_cells - visible_w)
            else:
                col = 0
        self._col_offset = max(0, col)
        self.refresh()

    def page_up(self) -> None:
        h = self._visible_height()
        self.sel_line = max(1, self.sel_line - h)
        self._ensure_visible(self.sel_line)
        self.refresh()

    def page_down(self) -> None:
        h = self._visible_height()
        n = self._line_count()
        self.sel_line = min(n if n else 1, self.sel_line + h)
        self._ensure_visible(self.sel_line)
        self.refresh()

    def half_page_up(self) -> None: self.scroll_up(self._visible_height() // 2)
    def half_page_down(self) -> None: self.scroll_down(self._visible_height() // 2)
    def goto_top(self) -> None: self.move_to(1)

    def goto_bottom(self, line: Optional[int] = None) -> None:
        self.move_to(line if line is not None else self._line_count())

    def goto_executing(self) -> None:
        if self.exe_line > 0:
            self._last_jump_line = self.sel_line
            self.move_to(self.exe_line)

    def goto_last_jump(self) -> None:
        tmp = self._last_jump_line
        self._last_jump_line = self.sel_line
        self.move_to(tmp)

    def show_logo(self) -> None:
        """Force logo display (:logo command)."""
        self._show_logo = True
        self.refresh()

    def goto_screen_top(self) -> None: self.move_to(self._scroll_top + 1)

    def goto_screen_middle(self) -> None:
        self.move_to(self._scroll_top + self._visible_height() // 2 + 1)

    def goto_screen_bottom(self) -> None:
        self.move_to(self._scroll_top + self._visible_height())

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, pattern: str, forward: bool = True,
               start: Optional[int] = None) -> bool:
        sf = self.source_file
        lines = sf.lines if sf else _LOGO_LINES
        if not lines or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False
        n = len(lines)
        s = (start if start is not None else self.sel_line) - 1
        if forward:
            order = list(range(s + 1, n)) + (list(range(0, s + 1)) if self.wrapscan else [])
        else:
            order = list(range(s - 1, -1, -1)) + (list(range(n - 1, s - 1, -1)) if self.wrapscan else [])
        for idx in order:
            if rx.search(lines[idx]):
                self._last_jump_line = self.sel_line
                self.move_to(idx + 1)
                return True
        return False

    def search_next(self) -> bool: return self.search(self._search_pattern, self._search_forward)
    def search_prev(self) -> bool: return self.search(self._search_pattern, not self._search_forward)

    def run_pending_search(self) -> bool:
        if not self._pending_search:
            return False
        pattern, forward = self._pending_search
        self._pending_search = None
        self._search_pattern = pattern
        self._search_forward = forward
        return self.search(pattern, forward)

    # ------------------------------------------------------------------
    # Marks
    # ------------------------------------------------------------------

    def set_mark(self, ch: str) -> None:
        sf = self.source_file
        if not sf:
            return
        if ch.islower():
            sf.marks_local[ch] = self.sel_line
        else:
            self._global_marks[ch] = (sf.path, self.sel_line)

    def jump_to_mark(self, ch: str) -> bool:
        sf = self.source_file
        if ch.islower():
            line = sf.marks_local.get(ch) if sf else None
            if line is not None:
                self._last_jump_line = self.sel_line
                self.move_to(line)
                return True
        else:
            mark = self._global_marks.get(ch)
            if mark:
                path, line = mark
                if sf and sf.path == path:
                    self._last_jump_line = self.sel_line
                    self.move_to(line)
                    return True
                else:
                    self.post_message(JumpGlobalMark(path, line))
                    return True
        return False

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

    def on_resize(self, event: events.Resize) -> None:
        self._ensure_visible(self.sel_line)
        self.refresh()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def handle_cgdb_key(self, key: str, char: str) -> bool:
        if self._search_active:
            self._handle_search_input(key, char)
            return True

        # 'g' double-press for gg (goto top)
        if self._await_g:
            self._await_g = False
            if char == "g":
                self._num_buf = ""
                self.goto_top()
                return True
            # Not 'gg' — treat buffered 'g' as nothing, reprocess current key
            self._num_buf = ""

        # Numeric prefix: 1-9 always starts/extends a count; 0 extends an
        # already-started count (e.g. "20j" → count 20) but alone means col-0.
        if char.isdigit() and (char != "0" or self._num_buf):
            self._num_buf += char
            return True
        has_prefix = bool(self._num_buf)
        count = int(self._num_buf) if self._num_buf else 1
        self._num_buf = ""

        consumed = True
        if key in ("j", "down"):
            self.scroll_down(count)
        elif key in ("k", "up"):
            self.scroll_up(count)
        elif key in ("h", "left"):
            self.scroll_col(-count)
        elif key in ("l", "right"):
            self.scroll_col(count)
        elif key in ("ctrl+f", "pagedown"):
            [self.page_down() for _ in range(count)]
        elif key in ("ctrl+b", "pageup"):
            [self.page_up() for _ in range(count)]
        elif key == "ctrl+d":
            self.half_page_down()
        elif key == "ctrl+u":
            self.half_page_up()
        elif key == "G":
            if has_prefix:
                n = self._line_count() or 1
                self.move_to(max(1, min(count, n)))
            else:
                self.goto_bottom()
        elif key == "H":
            self.goto_screen_top()
        elif key == "M":
            self.goto_screen_middle()
        elif key == "L":
            self.goto_screen_bottom()
        elif char == "g":
            self._await_g = True           # wait for second 'g'
        elif key == "slash":
            self._search_active = True
            self._search_forward = True
            self._search_buf = ""
            self.post_message(SearchStart(forward=True))
        elif key == "question_mark":
            self._search_active = True
            self._search_forward = False
            self._search_buf = ""
            self.post_message(SearchStart(forward=False))
        elif char == "n":
            if not self.search_next():
                self.post_message(StatusMessage("Pattern not found"))
        elif char == "N":
            if not self.search_prev():
                self.post_message(StatusMessage("Pattern not found"))
        elif key == "space":
            self.post_message(ToggleBreakpoint(self.sel_line))
        elif char == "t":
            self.post_message(ToggleBreakpoint(self.sel_line, temporary=True))
        elif char == "u":
            # cgdb source_input 'u': run until current cursor location
            sf2 = self.source_file
            if sf2:
                self.post_message(GDBCommand(f"until {sf2.path}:{self.sel_line}"))
        elif char == "o":
            self.post_message(OpenFileDialog())
        elif key == "colon" or char == ":":
            getattr(self.app, "_enter_cmd_mode", lambda: None)()
        elif key == "apostrophe":
            self.post_message(AwaitMarkJump())
        elif char == "m":
            self.post_message(AwaitMarkSet())
        elif key == "ctrl+l":
            self.app.refresh()
        elif key == "minus":
            self.post_message(ResizeSource(-1, rows=True))
        elif key in ("equal",) or char == "=":
            self.post_message(ResizeSource(1, rows=True))
        elif key == "underscore":
            self.post_message(ResizeSource(-1, jump=True))
        elif key == "plus":
            self.post_message(ResizeSource(1, jump=True))
        elif key == "ctrl+w":
            self.post_message(ToggleOrientation())
        elif key == "ctrl+t":
            self.post_message(OpenTTY())
        elif char == "0":
            # vim: '0' = go to beginning of line (column 0)
            self.scroll_col_to(0)
        elif char == "^":
            # vim: '^' = first visible char (same as 0 for source view)
            self.scroll_col_to(0)
        elif key == "dollar" or char == "$":
            # vim: '$' = go to end of line
            self.scroll_col_to(999999)
        elif key == "f1":
            self.post_message(ShowHelp())  # cgdb: if_display_help
        elif key == "f5":
            self.post_message(GDBCommand("run"))
        elif key == "f6":
            self.post_message(GDBCommand("continue"))
        elif key == "f7":
            self.post_message(GDBCommand("finish"))
        elif key == "f8":
            self.post_message(GDBCommand("next"))
        elif key == "f10":
            self.post_message(GDBCommand("step"))
        else:
            consumed = False

        return consumed

    def on_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character or ""

        if getattr(self.app, "_mode", None) == "CMD":
            from .command_line_bar import CommandLineBar

            try:
                status = self.app.query_one("#cmdline", CommandLineBar)
                status.feed_key(key, char)
            except Exception:
                pass
            event.stop()
            return

        if self.handle_cgdb_key(key, char):
            event.stop()

    def _handle_search_input(self, key: str, char: str) -> None:
        if key == "escape":
            self._search_active = False
            self.post_message(SearchCancel())
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            if self._search_pattern:
                if (
                    self.source_file is None
                    and getattr(self.app, "_initial_source_pending", False)
                ):
                    self._pending_search = (self._search_pattern, self._search_forward)
                else:
                    self.search(self._search_pattern, self._search_forward)
            self.post_message(SearchCommit(self._search_pattern))
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.post_message(SearchUpdate(self._search_buf))
        elif char and char.isprintable():
            self._search_buf += char
            self.post_message(SearchUpdate(self._search_buf))


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class ToggleBreakpoint(Message):
    def __init__(self, line: int, temporary: bool = False) -> None:
        super().__init__()
        self.line = line
        self.temporary = temporary


class OpenFileDialog(Message):
    pass


class AwaitMarkJump(Message):
    pass


class AwaitMarkSet(Message):
    pass


class JumpGlobalMark(Message):
    def __init__(self, path: str, line: int) -> None:
        super().__init__()
        self.path = path
        self.line = line


class SearchStart(Message):
    def __init__(self, forward: bool) -> None:
        super().__init__()
        self.forward = forward


class SearchUpdate(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern


class SearchCommit(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern


class SearchCancel(Message):
    pass


class StatusMessage(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ResizeSource(Message):
    """Request to resize the source/gdb split.

    rows=True : delta is ±1 row (cgdb '=' / '-')
    jump=True : delta is ±1 quarter-mark step (cgdb '+' / '_')
    """

    def __init__(self, delta: int, rows: bool = False, jump: bool = False,
                 percent: bool = False) -> None:
        super().__init__()
        self.delta = delta
        self.rows = rows
        self.jump = jump
        self.percent = percent   # legacy, kept for compatibility


class ToggleOrientation(Message):
    pass


class OpenTTY(Message):
    pass


class ShowHelp(Message):
    pass


class GDBCommand(Message):
    def __init__(self, cmd: str) -> None:
        super().__init__()
        self.cmd = cmd
