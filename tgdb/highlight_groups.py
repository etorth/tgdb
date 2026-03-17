"""Highlight group definitions — mirrors cgdb's highlight_groups.cpp."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HighlightStyle:
    """One highlight group (foreground, background, attributes)."""
    fg: Optional[str] = None      # Textual/Rich color name or None
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
        return " ".join(parts) if parts else "default"


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
    "brown": "dark_orange",
    "darkyellow": "dark_orange",
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
    return CGDB_COLORS.get(lower, lower)


# ---------------------------------------------------------------------------
# Default highlight group table  (name → HighlightStyle)
# ---------------------------------------------------------------------------
DEFAULT_GROUPS: dict[str, HighlightStyle] = {
    # Syntax
    "Statement":            HighlightStyle(fg="bright_yellow", bold=True),
    "Type":                 HighlightStyle(fg="bright_cyan", bold=True),
    "Constant":             HighlightStyle(fg="bright_red"),
    "Comment":              HighlightStyle(fg="bright_black", bold=True),
    "PreProc":              HighlightStyle(fg="bright_blue"),
    "Normal":               HighlightStyle(),
    # UI
    "StatusLine":           HighlightStyle(reverse=True),
    "Search":               HighlightStyle(fg="black", bg="bright_yellow"),
    "IncSearch":            HighlightStyle(fg="black", bg="bright_cyan"),
    # Selected line (cursor in source window)
    "SelectedLineArrow":    HighlightStyle(fg="bright_white", bold=True),
    "SelectedLineHighlight":HighlightStyle(reverse=True),
    "SelectedLineBlock":    HighlightStyle(reverse=True),
    "SelectedLineNr":       HighlightStyle(fg="bright_white", bold=True),
    # Executing line (where GDB stopped)
    "ExecutingLineArrow":   HighlightStyle(fg="bright_green", bold=True),
    "ExecutingLineHighlight":HighlightStyle(fg="black", bg="bright_green"),
    "ExecutingLineBlock":   HighlightStyle(fg="black", bg="bright_green"),
    "ExecutingLineNr":      HighlightStyle(fg="bright_green", bold=True),
    # Breakpoints
    "Breakpoint":           HighlightStyle(fg="bright_red", bold=True),
    "DisabledBreakpoint":   HighlightStyle(fg="bright_yellow"),
    # Misc
    "Logo":                 HighlightStyle(fg="bright_blue", bold=True),
    "Mark":                 HighlightStyle(fg="bright_cyan"),
    "ScrollModeStatus":     HighlightStyle(reverse=True),
    "LineNumber":           HighlightStyle(fg="bright_black"),
}


class HighlightGroups:
    """Runtime table of highlight groups, configurable via :highlight."""

    def __init__(self) -> None:
        self._groups: dict[str, HighlightStyle] = {
            k: HighlightStyle(**vars(v)) for k, v in DEFAULT_GROUPS.items()
        }

    def get(self, name: str) -> HighlightStyle:
        return self._groups.get(name, HighlightStyle())

    def set(self, name: str, *, fg: str = "", bg: str = "",
            attrs: str = "") -> None:
        """Apply :highlight command values to a group."""
        grp = self._groups.setdefault(name, HighlightStyle())
        if fg:
            grp.fg = resolve_color(fg) or None
        if bg:
            grp.bg = resolve_color(bg) or None
        for attr in (a.strip().lower() for a in attrs.split(",") if a.strip()):
            if attr in ("normal", "none"):
                grp.bold = grp.underline = grp.reverse = grp.italic = False
                grp.dim = grp.blink = False
            elif attr == "bold":
                grp.bold = True
            elif attr == "underline":
                grp.underline = True
            elif attr in ("reverse", "inverse"):
                grp.reverse = True
            elif attr == "italic":
                grp.italic = True
            elif attr == "dim":
                grp.dim = True
            elif attr == "blink":
                grp.blink = True
            elif attr == "standout":
                grp.bold = True

    def style(self, name: str) -> str:
        """Return a Rich style string for *name*."""
        return self._groups.get(name, HighlightStyle()).to_rich()
