"""
Public implementation of the disassembly-pane package.

``DisasmPane`` is a black-box disassembly viewer that mirrors GDB's
``layout asm`` window:

- The title bar shows ``Thread <id> (asm) In: <func>`` on the left and
  ``Lxx  PC: 0x...`` on the right, all in the CommandLine palette (tgdb
  puts pane status at the top, while plain gdb puts it at the bottom).
- Each line is formatted as ``ADDR  <func+offset>  INST`` with the columns
  aligned across the visible block.
- The currently-executing instruction is marked with a leading ``>`` and
  rendered in the ``ExecutingLineBlock`` highlight group, so it stands
  out the same way the source pane marks the current line.
- Vi-style ``j``/``k``/``gg``/``G`` navigation moves the keyboard cursor
  through the listing.
"""

import asyncio
from dataclasses import dataclass, field
from collections.abc import Callable

from pygments.lexers.asm import GasLexer
from pygments.token import Token
from rich.text import Text
from textual import events
from textual.widget import Widget

from ..highlight_groups import HighlightGroups
from ..pane_base import PaneBase


_ASM_TOKEN_GROUPS: list[tuple] = [
    (Token.Comment, "Comment"),
    (Token.Name.Function, "Statement"),
    (Token.Name.Builtin, "Statement"),
    (Token.Keyword, "Statement"),
    (Token.Name.Variable, "Type"),
    (Token.Name.Constant, "PreProc"),
    (Token.Name.Label, "PreProc"),
    (Token.Literal.Number, "Constant"),
    (Token.Literal.String, "Constant"),
    (Token.Literal, "Constant"),
    (Token.Punctuation, "Normal"),
]


def _asm_token_group(ttype) -> str:
    for tok, group in _ASM_TOKEN_GROUPS:
        if ttype in tok:
            return group
    return "Normal"


_ASM_LEXER = GasLexer(stripnl=False)


def _tokenize_inst(inst: str) -> list[tuple[str, str]]:
    """Return [(text, hl_group), ...] for an instruction string."""
    if not inst:
        return []
    spans: list[tuple[str, str]] = []
    for ttype, val in _ASM_LEXER.get_tokens(inst):
        if not val:
            continue
        if val.endswith("\n"):
            val = val[:-1]
            if not val:
                continue
        spans.append((val, _asm_token_group(ttype)))
    return spans


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

    def __init__(self, hl: HighlightGroups, pane: "DisasmPane", **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._pane = pane
        self.can_focus = True
        self._lines: list[DisasmLine] = []
        self._current_addr: str = ""
        self._scroll_top: int = 0
        self._selected: int = 0
        self._await_g: bool = False
        self._user_moved: bool = False


    def set_disasm(self, lines: list[DisasmLine], current_addr: str = "") -> None:
        self._lines = list(lines)
        self._current_addr = current_addr
        # Use ``current_addr`` (when provided) as the single source of truth
        # for the PC marker; clear any per-line ``is_current`` flags the
        # parser may have set so two different lines can never light up at
        # the same time.
        if current_addr:
            for line in self._lines:
                line.is_current = (line.addr == current_addr)
        pc_index = -1
        for i, line in enumerate(self._lines):
            if (current_addr and line.addr == current_addr) or line.is_current:
                pc_index = i
                break
        if pc_index < 0:
            self._user_moved = False
            self._selected = 0
            self._scroll_top = 0
        elif self._user_moved and 0 <= self._selected < len(self._lines):
            # Keep the user's cursor where they put it; only re-center if
            # the PC is no longer visible in the current viewport.
            height = max(1, self.size.height or 1)
            if not (self._scroll_top <= pc_index < self._scroll_top + height):
                self._center_on(pc_index)
        else:
            self._selected = pc_index
            self._center_on(pc_index)
        self.refresh()


    def update_pc(self, current_addr: str) -> bool:
        """Move just the PC marker without replacing the line list.

        Returns True when ``current_addr`` is found inside the cached lines,
        False otherwise (caller should fall back to a full refresh).
        """
        if not current_addr:
            return False
        target_index = -1
        for i, line in enumerate(self._lines):
            if line.addr == current_addr:
                target_index = i
                break
        if target_index < 0:
            return False
        # Clear any stale per-line is_current marks left over from earlier
        # parses; the active PC is tracked solely via ``_current_addr`` from
        # this point on.
        for line in self._lines:
            line.is_current = False
        self._lines[target_index].is_current = True
        self._current_addr = current_addr
        if not self._user_moved:
            self._selected = target_index
            self._center_on(target_index)
        self.refresh()
        return True


    def _center_on(self, index: int) -> None:
        height = max(1, self.size.height or 1)
        target = index - height // 2
        max_top = max(0, len(self._lines) - height)
        self._scroll_top = max(0, min(target, max_top))


    def on_resize(self, event: events.Resize) -> None:
        # Keep the PC line centered when the pane is first sized or resized
        # so the layout doesn't show the PC at the top after a stop.
        if self._current_addr:
            for i, line in enumerate(self._lines):
                if line.addr == self._current_addr or line.is_current:
                    self._center_on(i)
                    break
            self.refresh()


    def _ensure_visible(self) -> None:
        height = max(1, self.size.height or 1)
        if self._selected < self._scroll_top:
            self._scroll_top = self._selected
        elif self._selected >= self._scroll_top + height:
            self._scroll_top = self._selected - height + 1


    def _render_line(
        self,
        line: DisasmLine,
        is_pc: bool,
        is_selected: bool,
        addr_w: int,
        func_w: int,
        width: int,
    ) -> Text:
        if is_pc:
            row_style = self.hl.style("ExecutingLineBlock")
            colorize = False
        elif is_selected:
            row_style = self.hl.style("SelectedLineHighlight")
            colorize = False
        else:
            row_style = self.hl.style("Normal")
            colorize = True

        text = Text(no_wrap=True, overflow="crop", style=row_style)
        marker = ">" if is_pc else " "
        text.append(marker)
        text.append(line.addr.ljust(addr_w))
        text.append("  ")
        if line.func_name:
            func_part = f"<{line.func_name}+{line.offset}>"
        else:
            func_part = ""
        if func_part and colorize:
            text.append(func_part.ljust(func_w), style=self.hl.style("PreProc"))
        else:
            text.append(func_part.ljust(func_w))
        text.append("  ")
        if colorize:
            for span_text, group in _tokenize_inst(line.inst):
                text.append(span_text, style=self.hl.style(group))
        else:
            text.append(line.inst)

        if text.cell_len < width:
            text.append(" " * (width - text.cell_len))
        else:
            text.truncate(width, overflow="crop")
        return text


    def render(self) -> Text:
        width = max(1, self.size.width or 1)
        height = max(1, self.size.height or 1)
        result = Text(no_wrap=True, overflow="crop")
        visible = self._lines[self._scroll_top:self._scroll_top + height]
        addr_w = 0
        func_w = 0
        for line in visible:
            if len(line.addr) > addr_w:
                addr_w = len(line.addr)
            if line.func_name:
                func_part = f"<{line.func_name}+{line.offset}>"
                if len(func_part) > func_w:
                    func_w = len(func_part)
        for i, line in enumerate(visible):
            if i > 0:
                result.append("\n")
            is_pc = (
                self._current_addr != "" and line.addr == self._current_addr
            ) or line.is_current
            is_selected = (self._scroll_top + i) == self._selected
            result.append(
                self._render_line(line, is_pc, is_selected, addr_w, func_w, width)
            )
        remaining = height - len(visible)
        for _ in range(max(0, remaining)):
            result.append("\n")
            result.append(" " * width, style=self.hl.style("Normal"))
        return result


    def on_key(self, event: events.Key) -> None:
        key = event.key
        previous = self._selected
        if key in ("j", "down"):
            if self._lines:
                self._selected = min(len(self._lines) - 1, self._selected + 1)
            self._await_g = False
        elif key in ("k", "up"):
            self._selected = max(0, self._selected - 1)
            self._await_g = False
        elif key == "G":
            self._selected = max(0, len(self._lines) - 1)
            self._await_g = False
        elif key == "g":
            if self._await_g:
                self._selected = 0
                self._scroll_top = 0
                self._await_g = False
            else:
                self._await_g = True
            event.stop()
            self.refresh()
            return
        elif key in ("ctrl+f", "pagedown"):
            self._selected = min(
                max(0, len(self._lines) - 1),
                self._selected + max(1, self.size.height - 1),
            )
            self._await_g = False
        elif key in ("ctrl+b", "pageup"):
            self._selected = max(
                0, self._selected - max(1, self.size.height - 1)
            )
            self._await_g = False
        else:
            self._await_g = False
            return
        self._ensure_visible()
        if self._selected != previous:
            self._user_moved = True
            self._pane.refresh_title()
        self.refresh()
        event.stop()


class DisasmPane(PaneBase):
    """Render the current function's disassembly.

    Public interface
    ----------------
    ``DisasmPane(hl, **kwargs)``
        Create the widget.

    ``set_disasm(lines, current_addr="", thread_id="", func="")``
        Replace the visible disassembly with already-parsed lines and the
        current PC / thread / function used in the title bar.

    ``set_disasm_fn(fn)``
        Inject the async callback that asks GDB for disassembly data.

    ``refresh_disasm(filename, line, current_addr="", thread_id="", func="")``
        Query GDB for disassembly near the given source location and redraw
        the pane when the result arrives.
    """

    def align(self) -> str:
        return "left"


    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        """Create an empty disassembly pane."""
        super().__init__(hl, **kwargs)
        self._content = _DisasmContent(hl, self)
        self._disasm_fn: Callable | None = None
        self._disasm_pc_fn: Callable | None = None
        self._disasm_function_fn: Callable | None = None
        self._thread_id: str = ""
        self._func: str = ""


    def title(self) -> str:
        thread_part = "Thread"
        if self._thread_id:
            thread_part = f"Thread {self._thread_id}"
        if self._func:
            left = f"{thread_part} (asm) In: {self._func}"
        else:
            left = f"{thread_part} (asm)"

        if self._content._lines:
            line_index = self._content._selected + 1
            line_part = f"L{line_index}"
        else:
            line_part = ""

        pc = self._content._current_addr
        if pc:
            pc_part = f"PC: {pc}"
        else:
            pc_part = ""

        right_parts = [p for p in (line_part, pc_part) if p]
        right = "  ".join(right_parts)

        width = self._title_bar.size.width if self._title_bar else 0
        if not width:
            return left + ("  " + right if right else "")

        gap = max(2, width - len(left) - len(right))
        text = left + (" " * gap) + right
        if len(text) > width:
            text = text[:width]
        return text


    def compose(self):
        yield from super().compose()
        yield self._content


    def set_disasm(
        self,
        lines: list[DisasmLine],
        current_addr: str = "",
        thread_id: str = "",
        func: str = "",
    ) -> None:
        """Publish a parsed disassembly snapshot."""
        if thread_id:
            self._thread_id = thread_id
        if func:
            self._func = func
        self._content.set_disasm(lines, current_addr)
        self.refresh_title()


    def set_disasm_fn(self, fn: Callable) -> None:
        """Install the async callback used to request disassembly from GDB."""
        self._disasm_fn = fn


    def set_disasm_pc_fn(self, fn: Callable) -> None:
        """Install the async callback used to disassemble around an address.

        Used as a fallback when the current frame has no source file (libc,
        signal handlers, JIT) so the source-line based query is not usable.
        """
        self._disasm_pc_fn = fn


    def set_disasm_function_fn(self, fn: Callable) -> None:
        """Install the async callback used to disassemble a whole function.

        Used to prime the pane with ``main`` before the program has started.
        """
        self._disasm_function_fn = fn


    async def prime_function(self, spec: str) -> None:
        """Fill the empty pane with the disassembly of ``spec``.

        ``spec`` is anything GDB's ``-data-disassemble -a`` accepts: an
        address (``0x401120``) or a symbol name (``main``). Has no effect
        once the pane already shows something.
        """
        if self._content._lines:
            return
        if self._disasm_function_fn is None:
            return
        try:
            raw = await self._disasm_function_fn(spec)
        except Exception:
            raw = []
        if not raw:
            return
        if self._content._lines:
            return
        lines = _parse_disasm(raw, "")
        func = ""
        for entry in lines:
            if entry.func_name:
                func = entry.func_name
                break
        self.set_disasm(lines, current_addr="", func=func)


    def reset_user_cursor(self) -> None:
        """Forget any user-driven cursor movement so the next snapshot recenters."""
        self._content._user_moved = False


    async def refresh_disasm(
        self,
        filename: str,
        line: int,
        current_addr: str = "",
        thread_id: str = "",
        func: str = "",
    ) -> None:
        """Fetch and display disassembly near a source location."""
        if thread_id:
            self._thread_id = thread_id
        if func:
            self._func = func

        # Fast path: PC moved within the cached function — just shift the
        # marker without re-issuing the MI request. This avoids a round-trip
        # per ``nexti`` in tight loops.
        if (
            current_addr
            and (not func or func == self._func)
            and self._content.update_pc(current_addr)
        ):
            self.refresh_title()
            return

        if filename and self._disasm_fn is not None:
            try:
                raw = await self._disasm_fn(filename, line)
            except Exception:
                raw = []
        elif current_addr and self._disasm_pc_fn is not None:
            try:
                raw = await self._disasm_pc_fn(current_addr)
            except Exception:
                raw = []
        else:
            return

        lines = _parse_disasm(raw, current_addr)
        if not func and lines:
            for entry in lines:
                if entry.func_name:
                    func = entry.func_name
                    break
        self.set_disasm(
            lines, current_addr=current_addr, thread_id=thread_id, func=func,
        )


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

    first = raw[0]
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
