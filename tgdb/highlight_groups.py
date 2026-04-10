"""Highlight group definitions — mirrors cgdb's highlight_groups.cpp."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class HighlightStyle:
    """One highlight group (foreground, background, attributes)."""

    fg: Optional[str] = None  # Textual/Rich color name or None
    bg: Optional[str] = None
    bold: bool = False
    underline: bool = False
    reverse: bool = False
    italic: bool = False
    dim: bool = False
    blink: bool = False

    def to_rich(self) -> str:
        """Return a Rich markup style string."""
        parts: list[str] = []
        if self.bold:
            parts.append("bold")
        if self.underline:
            parts.append("underline")
        if self.reverse:
            parts.append("reverse")
        if self.italic:
            parts.append("italic")
        if self.dim:
            parts.append("dim")
        if self.blink:
            parts.append("blink")
        if self.fg:
            parts.append(self.fg)
        if self.bg:
            parts.append(f"on {self.bg}")
        if parts:
            return " ".join(parts)
        return "default"


# ---------------------------------------------------------------------------
# Colour name → Rich colour mapping (matches cgdb's curses colour names)
# ---------------------------------------------------------------------------
CGDB_COLORS: dict[str, str] = {
    "black": "black",
    "darkblue": "blue",
    "darkgreen": "green",
    "darkcyan": "cyan",
    "darkred": "red",
    "darkmagenta": "magenta",
    "brown": "yellow",
    "darkyellow": "yellow",
    "lightgray": "white",
    "lightgrey": "white",
    "gray": "white",
    "grey": "white",
    "darkgray": "bright_black",
    "darkgrey": "bright_black",
    "blue": "bright_blue",
    "lightblue": "bright_blue",
    "green": "bright_green",
    "lightgreen": "bright_green",
    "cyan": "bright_cyan",
    "lightcyan": "bright_cyan",
    "red": "bright_red",
    "lightred": "bright_red",
    "magenta": "bright_magenta",
    "lightmagenta": "bright_magenta",
    "yellow": "bright_yellow",
    "lightyellow": "bright_yellow",
    "white": "bright_white",
    # Allow raw rich/hex colours through
}


def resolve_color(name: str) -> str:
    """Normalise a cgdb colour name to a Rich colour string."""
    if name in ("-1", "none", ""):
        return ""
    lower = name.lower()
    if lower.lstrip("-").isdigit():
        number = int(lower)
        if number < 0:
            return ""
        return f"color({number})"
    return CGDB_COLORS.get(lower, lower)


# ---------------------------------------------------------------------------
# Default highlight group table  (name → HighlightStyle)
# ---------------------------------------------------------------------------
DEFAULT_GROUPS: dict[str, HighlightStyle] = {
    # Syntax — matches cgdb highlight_groups.cpp defaults exactly:
    # HLG_KEYWORD:   BOLD, COLOR_BLUE   → bold bright_blue
    # HLG_TYPE:      BOLD, COLOR_GREEN  → bold bright_green
    # HLG_LITERAL:   BOLD, COLOR_RED    → bold bright_red
    # HLG_COMMENT:   NORMAL, COLOR_YELLOW → yellow (no bold)
    # HLG_DIRECTIVE: BOLD, COLOR_CYAN   → bold bright_cyan
    # HLG_TEXT:      NORMAL, -1         → no style
    "Statement": HighlightStyle(fg="bright_blue", bold=True),
    "Type": HighlightStyle(fg="bright_green", bold=True),
    "Constant": HighlightStyle(fg="bright_red", bold=True),
    "Comment": HighlightStyle(fg="yellow"),
    "PreProc": HighlightStyle(fg="bright_cyan", bold=True),
    "Normal": HighlightStyle(),
    # UI — cgdb: HLG_STATUS_BAR=REVERSE, HLG_SEARCH=black on yellow,
    #           HLG_INCSEARCH=REVERSE
    # Formerly "StatusLine"; kept as alias for cgdb config-file compatibility.
    "CommandLine": HighlightStyle(reverse=True),
    "Search": HighlightStyle(fg="black", bg="yellow"),
    "IncSearch": HighlightStyle(reverse=True),
    # Selected line — explicit gray (color 240) so it is visually distinct
    # from the title bar's reverse-video white.
    "SelectedLineArrow": HighlightStyle(fg="bright_white", bold=True),
    "SelectedLineHighlight": HighlightStyle(
        fg="bright_white", bg="color(240)", bold=True
    ),
    "SelectedLineBlock": HighlightStyle(fg="bright_white", bg="color(240)"),
    "SelectedLineNr": HighlightStyle(fg="bright_white", bold=True),
    # Executing line — cgdb: HLG_EXECUTING_LINE_ARROW=bold green,
    #   HLG_EXECUTING_LINE_HIGHLIGHT=bold black on green,
    #   HLG_EXECUTING_LINE_BLOCK=reverse+green fg (bg becomes green when reversed)
    "ExecutingLineArrow": HighlightStyle(fg="bright_green", bold=True),
    "ExecutingLineHighlight": HighlightStyle(fg="black", bg="green", bold=True),
    "ExecutingLineBlock": HighlightStyle(fg="green", reverse=True),
    "ExecutingLineNr": HighlightStyle(fg="bright_green", bold=True),
    # Breakpoints — cgdb: both BOLD; enabled=red, disabled=yellow
    "Breakpoint": HighlightStyle(fg="bright_red", bold=True),
    "DisabledBreakpoint": HighlightStyle(fg="bright_yellow", bold=True),
    # Misc — cgdb: Logo=bold blue, Mark=bold white, ScrollModeStatus=bold (no color)
    "Logo": HighlightStyle(fg="bright_blue", bold=True),
    "Mark": HighlightStyle(fg="bright_white", bold=True),
    "ScrollModeStatus": HighlightStyle(bold=True),
    # tgdb-only (cgdb doesn't have a separate LineNumber group)
    "LineNumber": HighlightStyle(fg="bright_black"),
}


class HighlightGroups:
    """Runtime table of highlight groups, configurable via :highlight."""

    def __init__(self) -> None:
        self._groups: dict[str, HighlightStyle] = {
            k: HighlightStyle(**vars(v)) for k, v in DEFAULT_GROUPS.items()
        }

    # Legacy cgdb aliases — accepted in :highlight command
    _ALIASES: dict[str, str] = {
        "arrow": "ExecutingLineArrow",
        "linehighlight": "ExecutingLineHighlight",
        # cgdb config files use "StatusLine"; map to the new name
        "statusline": "CommandLine",
    }

    def _resolve_name(self, name: str) -> str:
        return self._ALIASES.get(name.lower(), name)

    def get(self, name: str) -> HighlightStyle:
        name = self._resolve_name(name)
        return self._groups.get(name, HighlightStyle())

    def set(self, name: str, *, fg: str = "", bg: str = "", attrs: str = "") -> None:
        """Apply :highlight command values to a group."""
        name = self._resolve_name(name)
        grp = self._groups.setdefault(name, HighlightStyle())
        if fg:
            grp.fg = resolve_color(fg) or None
        if bg:
            grp.bg = resolve_color(bg) or None

        # Map attribute name → (attr_name, value) to set on grp
        _ATTR_MAP: dict[str, tuple[str, bool]] = {
            "bold":      ("bold", True),
            "underline": ("underline", True),
            "reverse":   ("reverse", True),
            "inverse":   ("reverse", True),
            "italic":    ("italic", True),
            "dim":       ("dim", True),
            "blink":     ("blink", True),
            "standout":  ("bold", True),
        }
        for attr_raw in attrs.split(","):
            attr = attr_raw.strip().lower()
            if not attr:
                continue
            if attr in ("normal", "none"):
                grp.bold = grp.underline = grp.reverse = grp.italic = False
                grp.dim = grp.blink = False
            elif attr in _ATTR_MAP:
                field, val = _ATTR_MAP[attr]
                setattr(grp, field, val)

    def style(self, name: str) -> str:
        """Return a Rich style string for *name*."""
        name = self._resolve_name(name)
        return self._groups.get(name, HighlightStyle()).to_rich()
