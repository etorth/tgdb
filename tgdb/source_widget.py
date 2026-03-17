"""
Source view widget — mirrors cgdb's sources.cpp and interface.cpp source pane.

Features:
  • Syntax highlighting via pygments
  • Vi-like navigation (j/k, G/gg, Ctrl-b/f/u/d, H/M/L, h/l)
  • Breakpoint markers (B = enabled, b = disabled)
  • Executing line indicator (short/long arrow, highlight, block)
  • Selected line indicator (same styles)
  • Regex search (/ ? n N)
  • Marks (m[a-z/A-Z], '[a-z/A-Z], '', '.)
  • Auto-reload on file change
  • Line number display
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

from pygments import highlight as pg_highlight
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.formatters import TerminalTrueColorFormatter
from pygments.token import Token
from textual.app import ComposeResult
from textual.widget import Widget
from textual.geometry import Size
from textual import events
from rich.text import Text
from rich.style import Style

from .highlight_groups import HighlightGroups
from .gdb_controller import Breakpoint


# ---------------------------------------------------------------------------
# Pygments token → highlight group name
# ---------------------------------------------------------------------------
_TOKEN_TO_GROUP: dict = {
    Token.Keyword:              "Statement",
    Token.Keyword.Type:         "Type",
    Token.Name.Builtin:         "Statement",
    Token.Name.Builtin.Pseudo:  "Statement",
    Token.Literal.String:       "Constant",
    Token.Literal.Number:       "Constant",
    Token.Literal:              "Constant",
    Token.Comment:              "Comment",
    Token.Comment.Single:       "Comment",
    Token.Comment.Multiline:    "Comment",
    Token.Comment.Preproc:      "PreProc",
    Token.Comment.PreprocFile:  "PreProc",
    Token.Name.Decorator:       "PreProc",
}

def _token_group(ttype) -> str:
    for tok, group in _TOKEN_TO_GROUP.items():
        if ttype in tok:
            return group
    return "Normal"


# ---------------------------------------------------------------------------
# Line flags
# ---------------------------------------------------------------------------
BP_NONE     = 0
BP_ENABLED  = 1
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
        self.marks_local: dict[str, int] = {}   # 'a'-'z' → 0-based line
        self._tokens: Optional[list[list[tuple]]] = None  # cached token spans

    def tokenize(self, tabstop: int = 8) -> list[list[tuple]]:
        """Return per-line list of (text, group) spans, cached."""
        if self._tokens is not None:
            return self._tokens
        src = "\n".join(self.lines)
        try:
            lexer = get_lexer_for_filename(self.path, stripnl=False)
        except Exception:
            lexer = TextLexer(stripnl=False)
        tokens_flat: list[tuple] = list(lexer.get_tokens(src))
        # Build line-by-line spans
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
        # Pad/trim to match line count
        while len(lines_toks) < len(self.lines):
            lines_toks.append([])
        self._tokens = lines_toks[:len(self.lines)]
        return self._tokens


# ---------------------------------------------------------------------------
# Source view widget
# ---------------------------------------------------------------------------

class SourceView(Widget):
    """
    Scrollable, syntax-highlighted source viewer with vi-like keybindings.

    Set .source_file to a SourceFile to display it.
    Set .exe_line (1-based) for the executing line arrow.
    Set .highlight_groups to a HighlightGroups instance.
    """

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
        self.exe_line: int = 0          # 1-based; 0 = none
        self.sel_line: int = 1          # 1-based cursor
        self._scroll_top: int = 0       # 0-based index of first visible line
        self._search_pattern: str = ""
        self._search_forward: bool = True
        self._search_active: bool = False  # currently typing search
        self._search_buf: str = ""
        self.tabstop: int = 8
        self.executing_line_display: str = "longarrow"
        self.selected_line_display: str = "block"
        self.hlsearch: bool = False
        self.ignorecase: bool = False
        self.wrapscan: bool = True
        self.showmarks: bool = True
        # Global marks: uppercase letters stored in the app
        self._global_marks: dict[str, tuple[str, int]] = {}  # mark → (path, line)
        self._last_jump_line: int = 1
        # Number prefix accumulator (for e.g. 5j)
        self._num_buf: str = ""
        # can_focus
        self.can_focus = True

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def load_file(self, path: str) -> bool:
        """Load a source file. Returns True on success."""
        try:
            with open(path, errors="replace") as f:
                lines = f.read().expandtabs(self.tabstop).splitlines()
            sf = SourceFile(path, lines)
            # Preserve breakpoints if same file
            if self.source_file and self.source_file.path == path:
                sf.bp_flags = list(self.source_file.bp_flags[:len(lines)])
                while len(sf.bp_flags) < len(lines):
                    sf.bp_flags.append(BP_NONE)
                sf.marks_local = dict(self.source_file.marks_local)
            self.source_file = sf
            self.sel_line = max(1, min(self.sel_line, len(lines)))
            self.refresh()
            return True
        except OSError:
            return False

    def reload_if_changed(self) -> bool:
        """Reload source file if mtime changed. Returns True if reloaded."""
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
        """Update breakpoint markers from GDB breakpoint list."""
        sf = self.source_file
        if not sf:
            return
        sf.bp_flags = [BP_NONE] * len(sf.lines)
        for bp in bps:
            fullname = bp.fullname or bp.file
            if fullname and (os.path.abspath(fullname) == os.path.abspath(sf.path)
                             or os.path.basename(fullname) == os.path.basename(sf.path)):
                if 1 <= bp.line <= len(sf.lines):
                    sf.bp_flags[bp.line - 1] = BP_ENABLED if bp.enabled else BP_DISABLED
        self.refresh()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _line_count(self) -> int:
        return len(self.source_file.lines) if self.source_file else 0

    def _visible_height(self) -> int:
        return max(1, self.size.height)

    def _ensure_visible(self, line: int) -> None:
        """Scroll so 1-based *line* is visible."""
        h = self._visible_height()
        idx = line - 1
        if idx < self._scroll_top:
            self._scroll_top = idx
        elif idx >= self._scroll_top + h:
            self._scroll_top = idx - h + 1
        self._scroll_top = max(0, min(self._scroll_top,
                                      max(0, self._line_count() - h)))

    def move_to(self, line: int) -> None:
        n = self._line_count()
        self.sel_line = max(1, min(line, n if n else 1))
        self._ensure_visible(self.sel_line)
        self.refresh()

    def scroll_up(self, n: int = 1) -> None:
        self.move_to(self.sel_line - n)

    def scroll_down(self, n: int = 1) -> None:
        self.move_to(self.sel_line + n)

    def page_up(self) -> None:
        h = self._visible_height()
        self._scroll_top = max(0, self._scroll_top - h)
        self.sel_line = max(1, self.sel_line - h)
        self.refresh()

    def page_down(self) -> None:
        h = self._visible_height()
        n = self._line_count()
        self._scroll_top = min(max(0, n - h), self._scroll_top + h)
        self.sel_line = min(n if n else 1, self.sel_line + h)
        self.refresh()

    def half_page_up(self) -> None:
        self.scroll_up(self._visible_height() // 2)

    def half_page_down(self) -> None:
        self.scroll_down(self._visible_height() // 2)

    def goto_top(self) -> None:
        self.move_to(1)

    def goto_bottom(self, line: Optional[int] = None) -> None:
        self.move_to(line if line is not None else self._line_count())

    def goto_executing(self) -> None:
        if self.exe_line > 0:
            self._last_jump_line = self.sel_line
            self.move_to(self.exe_line)

    def goto_last_jump(self) -> None:
        line = self._last_jump_line
        self._last_jump_line = self.sel_line
        self.move_to(line)

    # Screen-relative navigation
    def goto_screen_top(self) -> None:
        self.move_to(self._scroll_top + 1)

    def goto_screen_middle(self) -> None:
        mid = self._scroll_top + self._visible_height() // 2
        self.move_to(mid + 1)

    def goto_screen_bottom(self) -> None:
        bottom = self._scroll_top + self._visible_height() - 1
        self.move_to(bottom + 1)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, pattern: str, forward: bool = True,
               start: Optional[int] = None) -> bool:
        """Search for *pattern*, move cursor to match. Returns True on found."""
        sf = self.source_file
        if not sf or not pattern:
            return False
        flags = re.IGNORECASE if self.ignorecase else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return False

        n = len(sf.lines)
        start_line = (start if start is not None else self.sel_line) - 1  # 0-based
        indices = list(range(n))
        if forward:
            order = indices[start_line + 1:] + (indices[:start_line + 1] if self.wrapscan else [])
        else:
            order = indices[:start_line][::-1] + (indices[start_line:][::-1] if self.wrapscan else [])

        for idx in order:
            if rx.search(sf.lines[idx]):
                self._last_jump_line = self.sel_line
                self.move_to(idx + 1)
                return True
        return False

    def search_next(self) -> bool:
        return self.search(self._search_pattern, self._search_forward)

    def search_prev(self) -> bool:
        return self.search(self._search_pattern, not self._search_forward)

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
                    # Post message to app to switch file
                    self.post_message(JumpGlobalMark(path, line))
                    return True
        return False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _build_rich_line(self, y: int) -> Text:
        """Build a Rich Text for visible row y."""
        sf = self.source_file
        if not sf:
            return Text("")

        line_idx = self._scroll_top + y   # 0-based
        if line_idx >= len(sf.lines):
            return Text("")

        line_no = line_idx + 1           # 1-based
        is_exe = (line_no == self.exe_line)
        is_sel = (line_no == self.sel_line)
        bp_flag = sf.bp_flags[line_idx] if line_idx < len(sf.bp_flags) else BP_NONE

        # --- Build the left gutter (line number + BP marker + arrow) ---
        line_num_str = f"{line_no:4d}"
        # BP marker column
        if bp_flag == BP_ENABLED:
            bp_char = "B"
            bp_style = self.hl.style("Breakpoint")
        elif bp_flag == BP_DISABLED:
            bp_char = "b"
            bp_style = self.hl.style("DisabledBreakpoint")
        else:
            bp_char = " "
            bp_style = ""

        # Mark indicator
        mark_char = " "
        if self.showmarks and sf:
            for mk, ml in sf.marks_local.items():
                if ml == line_no:
                    mark_char = mk
            if mark_char == " ":
                for mk, (mp, ml) in self._global_marks.items():
                    if mp == sf.path and ml == line_no:
                        mark_char = mk

        # Arrow for executing / selected line
        exe_display = self.executing_line_display
        sel_display = self.selected_line_display

        if is_exe:
            nr_style = self.hl.style("ExecutingLineNr")
        elif is_sel:
            nr_style = self.hl.style("SelectedLineNr")
        else:
            nr_style = self.hl.style("LineNumber")

        if is_exe:
            arrow = ">" if exe_display in ("shortarrow", "longarrow") else " "
            arr_style = self.hl.style("ExecutingLineArrow")
        elif is_sel:
            arrow = ">" if sel_display in ("shortarrow", "longarrow") else " "
            arr_style = self.hl.style("SelectedLineArrow")
        else:
            arrow = " "
            arr_style = ""

        result = Text(no_wrap=True, overflow="crop")
        result.append(line_num_str, style=nr_style)
        if bp_char != " ":
            result.append(bp_char, style=bp_style)
        else:
            result.append(mark_char, style=self.hl.style("Mark") if mark_char != " " else "")
        result.append(arrow, style=arr_style)
        result.append("|")

        # Long arrow fill
        long_arrow_fill = ""
        if is_exe and exe_display == "longarrow":
            long_arrow_fill = "-"
        elif is_sel and sel_display == "longarrow":
            long_arrow_fill = "-"

        # --- Source text ---
        tokens = sf.tokenize(self.tabstop)
        spans = tokens[line_idx] if line_idx < len(tokens) else []

        if not spans:
            spans = [(sf.lines[line_idx], "Normal")]

        # Determine line background styling
        if is_exe:
            if exe_display == "highlight":
                line_bg_style = self.hl.style("ExecutingLineHighlight")
            elif exe_display == "block":
                line_bg_style = self.hl.style("ExecutingLineBlock")
            else:
                line_bg_style = ""
        elif is_sel:
            if sel_display == "highlight":
                line_bg_style = self.hl.style("SelectedLineHighlight")
            elif sel_display == "block":
                line_bg_style = self.hl.style("SelectedLineBlock")
            else:
                line_bg_style = ""
        else:
            line_bg_style = ""

        if long_arrow_fill:
            result.append(long_arrow_fill * 2, style=arr_style)

        src_text = Text(no_wrap=True, overflow="crop")
        for tok_text, group in spans:
            tok_style = self.hl.style(group)
            if line_bg_style:
                src_text.append(tok_text, style=line_bg_style)
            else:
                src_text.append(tok_text, style=tok_style)

        # Hlsearch highlights
        if self.hlsearch and self._search_pattern:
            try:
                flags = re.IGNORECASE if self.ignorecase else 0
                rx = re.compile(self._search_pattern, flags)
                plain = src_text.plain
                for m in rx.finditer(plain):
                    src_text.stylize(self.hl.style("Search"),
                                     m.start(), m.end())
            except re.error:
                pass

        result.append_text(src_text)
        return result

    def render_line(self, y: int) -> "Strip":
        from textual.strip import Strip
        from rich.console import Console
        from rich.segment import Segment
        text = self._build_rich_line(y)
        width = self.size.width or 80
        # Render text to segments
        console = Console(width=width, highlight=False)
        segments = list(console.render(text, console.options.update_width(width)))
        # Remove trailing newline segment
        segments = [s for s in segments if s.text != "\n"]
        return Strip(segments, width)

    def render(self) -> "Text":
        """Fallback render (used when render_line is not called)."""
        lines: list[Text] = []
        h = self._visible_height()
        for y in range(h):
            lines.append(self._build_rich_line(y))
        sep = Text("\n")
        result = Text()
        for i, ln in enumerate(lines):
            result.append_text(ln)
            if i < len(lines) - 1:
                result.append("\n")
        return result

    # Textual rendering hook
    def on_resize(self, event: events.Resize) -> None:
        self._ensure_visible(self.sel_line)
        self.refresh()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> bool:
        """Handle vi-like keys. Return True if consumed."""
        key = event.key
        char = event.character or ""

        # Search input mode
        if self._search_active:
            return self._handle_search_input(key, char)

        # Numeric prefix
        if char.isdigit() and char != "0":
            self._num_buf += char
            event.stop()
            return True
        count = int(self._num_buf) if self._num_buf else 1
        self._num_buf = ""

        if key in ("j", "down"):
            self.scroll_down(count)
        elif key in ("k", "up"):
            self.scroll_up(count)
        elif key in ("h", "left"):
            pass  # horizontal scroll not implemented
        elif key in ("l", "right"):
            pass
        elif key in ("ctrl+f", "pagedown"):
            for _ in range(count): self.page_down()
        elif key in ("ctrl+b", "pageup"):
            for _ in range(count): self.page_up()
        elif key == "ctrl+d":
            self.half_page_down()
        elif key == "ctrl+u":
            self.half_page_up()
        elif key == "G":
            self.goto_bottom(count if self._num_buf == "" and count != 1 else None)
            # If count was 1 and no prefix, go to end
            if count == 1:
                self.goto_bottom()
        elif key == "H":
            self.goto_screen_top()
        elif key == "M":
            self.goto_screen_middle()
        elif key == "L":
            self.goto_screen_bottom()
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
        elif key == "n":
            if not self.search_next():
                self.post_message(StatusMessage("Pattern not found"))
        elif key == "N":
            if not self.search_prev():
                self.post_message(StatusMessage("Pattern not found"))
        elif key == "space":
            self.post_message(ToggleBreakpoint(self.sel_line))
        elif key == "t":
            self.post_message(ToggleBreakpoint(self.sel_line, temporary=True))
        elif key == "o":
            self.post_message(OpenFileDialog())
        elif key == "apostrophe":
            # Next keypress will be mark name
            self.post_message(AwaitMarkJump())
        elif key == "m":
            self.post_message(AwaitMarkSet())
        elif char == "g":
            # 'gg' — handled as double-g; simplified: just go top
            self.goto_top()
        elif key == "period" and char == ".":
            self.goto_executing()
        elif key == "ctrl+l":
            self.refresh()
        elif key == "minus":
            self.post_message(ResizeSource(-1))
        elif key in ("equal", "plus"):
            self.post_message(ResizeSource(1))
        elif key == "underscore":
            self.post_message(ResizeSource(-25, percent=True))
        elif key == "ctrl+w":
            self.post_message(ToggleOrientation())
        elif key == "ctrl+t":
            self.post_message(OpenTTY())
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
            return False
        event.stop()
        return True

    def _handle_search_input(self, key: str, char: str) -> bool:
        if key == "escape":
            self._search_active = False
            self.post_message(SearchCancel())
        elif key in ("enter", "return"):
            self._search_active = False
            self._search_pattern = self._search_buf
            if self._search_pattern:
                self.search(self._search_pattern, self._search_forward)
            self.post_message(SearchCommit(self._search_pattern))
        elif key in ("backspace", "ctrl+h"):
            self._search_buf = self._search_buf[:-1]
            self.post_message(SearchUpdate(self._search_buf))
        elif char and char.isprintable():
            self._search_buf += char
            self.post_message(SearchUpdate(self._search_buf))
        return True


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

from textual.message import Message

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
    def __init__(self, delta: int, percent: bool = False) -> None:
        super().__init__()
        self.delta = delta
        self.percent = percent

class ToggleOrientation(Message):
    pass

class OpenTTY(Message):
    pass

class GDBCommand(Message):
    def __init__(self, cmd: str) -> None:
        super().__init__()
        self.cmd = cmd
