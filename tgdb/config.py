"""
Configuration parser — mirrors cgdb's cgdbrc.cpp.

Parses ~/.cgdb/cgdbrc (or $CGDB_DIR/cgdbrc).
Supports all :set options, :highlight, :map, :imap, :unmap, :iunmap.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .highlight_groups import HighlightGroups
    from .key_mapper import KeyMapper


# ---------------------------------------------------------------------------
# Config state
# ---------------------------------------------------------------------------

@dataclass
class Config:
    # Boolean options
    autosourcereload: bool = True
    color: bool = True
    debugwincolor: bool = True
    disasm: bool = False
    hlsearch: bool = False
    ignorecase: bool = False
    showmarks: bool = True
    showdebugcommands: bool = False
    timeout: bool = True
    ttimeout: bool = True
    wrapscan: bool = True

    # Integer options
    scrollbackbuffersize: int = 10000
    tabstop: int = 8
    timeoutlen: int = 1000
    ttimeoutlen: int = 100
    winminheight: int = 0
    winminwidth: int = 0

    # String / enum options
    cgdbmodekey: str = "escape"     # key name
    executinglinedisplay: str = "longarrow"   # shortarrow|longarrow|highlight|block
    selectedlinedisplay: str = "block"
    winsplit: str = "even"          # src_full|src_big|even|gdb_big|gdb_full
    winsplitorientation: str = "horizontal"   # horizontal|vertical
    syntax: str = "on"              # on|off|c|asm|…


# Abbreviation → canonical name
_ALIASES: dict[str, str] = {
    "asr": "autosourcereload",
    "dwc": "debugwincolor",
    "dis": "disasm",
    "eld": "executinglinedisplay",
    "hls": "hlsearch",
    "ic":  "ignorecase",
    "sbbs": "scrollbackbuffersize",
    "sld": "selectedlinedisplay",
    "sdc": "showdebugcommands",
    "syn": "syntax",
    "to":  "timeout",
    "tm":  "timeoutlen",
    "ttm": "ttimeoutlen",
    "ts":  "tabstop",
    "wmh": "winminheight",
    "wmw": "winminwidth",
    "wso": "winsplitorientation",
    "ws":  "wrapscan",
}

_BOOL_OPTIONS = {
    "autosourcereload", "color", "debugwincolor", "disasm", "hlsearch",
    "ignorecase", "showmarks", "showdebugcommands", "timeout", "ttimeout",
    "wrapscan",
}
_INT_OPTIONS = {
    "scrollbackbuffersize", "tabstop", "timeoutlen", "ttimeoutlen",
    "winminheight", "winminwidth",
}
_STR_OPTIONS = {
    "cgdbmodekey", "executinglinedisplay", "selectedlinedisplay",
    "winsplit", "winsplitorientation", "syntax",
}


class ConfigParser:
    """
    Parses cgdbrc-style config commands and updates a Config object.

    Pass in the live Config, HighlightGroups, and KeyMapper objects.
    """

    def __init__(self,
                 config: Config,
                 highlight_groups: "HighlightGroups",
                 key_mapper: "KeyMapper") -> None:
        self.config = config
        self.hl = highlight_groups
        self.km = key_mapper
        # Additional command handlers registered by the app
        self._handlers: dict[str, Callable[[list[str]], Optional[str]]] = {}

    def register_handler(self, name: str,
                         fn: Callable[[list[str]], Optional[str]]) -> None:
        """Register an extra command handler (e.g., GDB debug commands)."""
        self._handlers[name] = fn

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def load_default_rc(self) -> None:
        cgdb_dir = os.environ.get("CGDB_DIR", "")
        candidates = []
        if cgdb_dir:
            candidates.append(Path(cgdb_dir) / "cgdbrc")
        home = Path.home()
        candidates.append(home / ".cgdb" / "cgdbrc")
        for path in candidates:
            if path.exists():
                self.load_file(str(path))
                return

    def load_file(self, path: str) -> None:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    self.execute(line)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute(self, line: str) -> Optional[str]:
        """
        Execute one config/status-bar command.
        Returns an error string or None on success.
        """
        line = line.strip()
        if not line:
            return None
        # Strip leading colon
        if line.startswith(":"):
            line = line[1:].strip()
        if not line:
            return None

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return None

        cmd = parts[0].lower()
        args = parts[1:]

        # Check registered handlers first
        if cmd in self._handlers:
            return self._handlers[cmd](args)

        # Built-in commands
        if cmd == "set":
            return self._cmd_set(args)
        elif cmd in ("highlight", "hi"):
            return self._cmd_highlight(args)
        elif cmd in ("map",):
            return self._cmd_map("cgdb", args)
        elif cmd in ("imap", "im"):
            return self._cmd_map("gdb", args)
        elif cmd in ("unmap", "unm"):
            return self._cmd_unmap("cgdb", args)
        elif cmd in ("iunmap", "iu"):
            return self._cmd_unmap("gdb", args)
        elif cmd == "noh":
            self.config.hlsearch = False
            return None
        elif cmd == "syntax":
            if args:
                return self._set_option("syntax", args[0])
        return f"Unknown command: {cmd}"

    # ------------------------------------------------------------------
    # :set
    # ------------------------------------------------------------------

    def _cmd_set(self, args: list[str]) -> Optional[str]:
        if not args:
            return "set: missing argument"
        expr = args[0]
        # Boolean negation: noXXX
        if expr.startswith("no"):
            name = self._resolve_name(expr[2:])
            if name in _BOOL_OPTIONS:
                setattr(self.config, name, False)
                return None
        # Assignment: name=value
        if "=" in expr:
            name, _, value = expr.partition("=")
            name = self._resolve_name(name.strip())
            return self._set_option(name, value.strip())
        # Boolean enable
        name = self._resolve_name(expr)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, True)
            return None
        return f"set: unknown option '{expr}'"

    def _set_option(self, name: str, value: str) -> Optional[str]:
        name = self._resolve_name(name)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, value.lower() not in ("0", "false", "off", "no"))
            return None
        elif name in _INT_OPTIONS:
            try:
                setattr(self.config, name, int(value))
                # Propagate timeout values to key mapper
                if name == "timeoutlen":
                    self.km.timeout_ms = int(value)
                elif name == "ttimeoutlen":
                    self.km.ttimeout_ms = int(value)
            except ValueError:
                return f"set: invalid integer '{value}'"
            return None
        elif name in _STR_OPTIONS:
            setattr(self.config, name, value.lower())
            return None
        return f"set: unknown option '{name}'"

    def _resolve_name(self, name: str) -> str:
        return _ALIASES.get(name.lower(), name.lower())

    # ------------------------------------------------------------------
    # :highlight / :hi
    # ------------------------------------------------------------------

    def _cmd_highlight(self, args: list[str]) -> Optional[str]:
        if not args:
            return "highlight: missing group name"
        group = args[0]
        fg = bg = attrs_val = ""
        for tok in args[1:]:
            if "=" in tok:
                k, _, v = tok.partition("=")
                k = k.lower()
                if k == "ctermfg":
                    fg = v
                elif k == "ctermbg":
                    bg = v
                elif k in ("cterm", "term"):
                    attrs_val = v
        self.hl.set(group, fg=fg, bg=bg, attrs=attrs_val)
        return None

    # ------------------------------------------------------------------
    # :map / :imap
    # ------------------------------------------------------------------

    def _cmd_map(self, mode: str, args: list[str]) -> Optional[str]:
        if len(args) < 2:
            return "map: requires lhs and rhs"
        lhs = self._decode_keyseq(args[0])
        rhs = self._decode_keyseq(args[1])
        self.km.map(mode, lhs, rhs)
        return None

    def _cmd_unmap(self, mode: str, args: list[str]) -> Optional[str]:
        if not args:
            return "unmap: requires lhs"
        lhs = self._decode_keyseq(args[0])
        self.km.unmap(mode, lhs)
        return None

    def _decode_keyseq(self, s: str) -> str:
        """
        Decode cgdb key notation: <F5>, <C-w>, <Space>, <Enter>, etc.
        Returns a single string of "logical" key characters.
        """
        result = []
        i = 0
        while i < len(s):
            if s[i] == "<":
                end = s.find(">", i)
                if end != -1:
                    token = s[i+1:end].lower()
                    result.append(self._keyname(token))
                    i = end + 1
                    continue
            result.append(s[i])
            i += 1
        return "".join(result)

    _KEY_NAMES = {
        "space": " ", "enter": "\r", "return": "\r", "cr": "\r",
        "nl": "\n", "tab": "\t", "esc": "\x1b", "escape": "\x1b",
        "bs": "\x08", "backspace": "\x08", "del": "\x7f",
        "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
        "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
        "home": "\x1b[H", "end": "\x1b[F",
        "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
        "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~",
        "f8": "\x1b[19~", "f9": "\x1b[20~", "f10": "\x1b[21~",
        "f11": "\x1b[23~", "f12": "\x1b[24~",
    }

    def _keyname(self, token: str) -> str:
        if token in self._KEY_NAMES:
            return self._KEY_NAMES[token]
        # C-x → Ctrl char
        if token.startswith("c-") and len(token) == 3:
            ch = token[2]
            return chr(ord(ch.upper()) - 64)
        # M-x → Alt (ESC + char)
        if token.startswith("m-") and len(token) == 3:
            return "\x1b" + token[2]
        return token
