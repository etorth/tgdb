"""
Configuration types and constants for tgdb.

This module defines the public ``Config`` state object, ``UserCommandDef``, and
the option/alias constants used internally by the configuration package.
"""

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Reserved namespace prefix — any name starting with this string is internal
# and must never leak into or be overwritten from user Python scripts.
# ---------------------------------------------------------------------------
_TGDB_RESERVED_PREFIX = "_tgdb_RSVD"

# ---------------------------------------------------------------------------
# Config state
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Mutable runtime configuration shared between tgdb subsystems."""

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

    # History options
    historysize: int = 1024  # set history=N  (0 = disabled)

    # Integer options
    expandchildlimit: int = 0  # set expandchildlimit=N  (0 = no limit, load all at once)
    scrollbackbuffersize: int = 10000
    tabstop: int = 8
    timeoutlen: int = 1000
    ttimeoutlen: int = 100
    winminheight: int = 0
    winminwidth: int = 0

    # String / enum options
    tgdbmodekey: str = "escape"  # key name
    executinglinedisplay: str = "longarrow"  # shortarrow|longarrow|highlight|block
    selectedlinedisplay: str = "block"
    winsplit: str = "even"  # src_full|src_big|even|gdb_big|gdb_full
    winsplitorientation: str = "vertical"  # horizontal|vertical
    syntax: str = "on"  # on|off|c|asm|…

    # Path options (stored verbatim — no lowercasing)
    clipboardpath: str = ""  # e.g. /usr/local/bin/xclip


_BOOL_OPTIONS = {
    "autosourcereload",
    "color",
    "debugwincolor",
    "disasm",
    "hlsearch",
    "ignorecase",
    "showmarks",
    "showdebugcommands",
    "timeout",
    "ttimeout",
    "wrapscan",
}
_INT_OPTIONS = {
    "expandchildlimit",
    "historysize",
    "scrollbackbuffersize",
    "tabstop",
    "timeoutlen",
    "ttimeoutlen",
    "winminheight",
    "winminwidth",
}
_STR_OPTIONS = {
    "tgdbmodekey",
    "executinglinedisplay",
    "selectedlinedisplay",
    "winsplit",
    "winsplitorientation",
    "syntax",
}
# Path options: stored verbatim (no lowercasing); setting one may have side-effects.
_PATH_OPTIONS = {"clipboardpath"}

# Valid -nargs values for :command
_VALID_NARGS = {"0", "1", "*", "?", "+"}

# User command name must start with uppercase, rest uppercase/lowercase/digits
_CMD_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")


@dataclass
class UserCommandDef:
    """One user-defined command registered via :command."""

    name: str
    nargs: str  # "0" | "1" | "*" | "?" | "+"
    complete_func: str  # name of Python function in _py_namespace, or ""
    replacement: str  # raw replacement template with <args>/<q-args>/<f-args>/<lt>
