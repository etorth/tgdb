"""Shared helpers and constants for the configuration package."""

from __future__ import annotations

import os


_ALIASES: dict[str, str] = {
    "asr": "autosourcereload",
    "arrowstyle": "executinglinedisplay",
    "as": "executinglinedisplay",
    "dwc": "debugwincolor",
    "dis": "disasm",
    "ecl": "expandchildlimit",
    "eld": "executinglinedisplay",
    "hls": "hlsearch",
    "ic": "ignorecase",
    "sbbs": "scrollbackbuffersize",
    "sld": "selectedlinedisplay",
    "sdc": "showdebugcommands",
    "syn": "syntax",
    "to": "timeout",
    "tm": "timeoutlen",
    "ttm": "ttimeoutlen",
    "ts": "tabstop",
    "wmh": "winminheight",
    "wmw": "winminwidth",
    "wso": "winsplitorientation",
    "ws": "wrapscan",
}


def _apply_clipboard_path(path: str) -> None:
    """Apply a clipboardpath setting immediately."""
    dirname = os.path.dirname(path)
    basename = os.path.basename(path)
    if dirname:
        current = os.environ.get("PATH", "")
        parts = current.split(os.pathsep)
        if dirname not in parts:
            os.environ["PATH"] = dirname + os.pathsep + current
    if basename:
        try:
            import pyperclip

            pyperclip.set_clipboard(basename)
        except Exception:
            pass
