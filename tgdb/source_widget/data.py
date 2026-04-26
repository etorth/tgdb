"""Data structures and helpers for source file handling."""

import os

from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.token import Token

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
        self.marks_local: dict[str, int] = {}  # 'a'-'z' → 1-based line
        self._tokens: list[list[tuple]] | None = None


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
        self._tokens = lines_toks[: len(self.lines)]
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
