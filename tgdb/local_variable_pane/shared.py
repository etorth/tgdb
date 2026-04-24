"""
Shared helpers and type aliases for the local-variable pane modules.
"""

from __future__ import annotations

import logging
import re
from typing import TypeAlias

from ..gdb_controller import LocalVariable

_log = logging.getLogger("tgdb.locals")

_SHADOW_SUFFIX = "  ← shadowed"
_ACCESS_SPECIFIERS = {"public", "private", "protected"}
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")

BindingKey: TypeAlias = tuple[str, str]
BindingEntry: TypeAlias = tuple[str, str, LocalVariable]
ExpansionSegment: TypeAlias = tuple[str, int]
ExpansionPath: TypeAlias = tuple[ExpansionSegment, ...]
FrameKey: TypeAlias = tuple[str, str, frozenset[BindingKey]] | None


def _suppress_children(varobj_info: dict) -> bool:
    """Return True when the varobj should be shown as a non-expandable leaf.

    GDB's pretty-printer framework sets ``displayhint = "string"`` for every
    string-like type whose printer returns ``display_hint() = 'string'``,
    including ``std::string`` / ``std::wstring`` / ``std::u8string`` /
    ``std::u16string`` / ``std::u32string`` and any future string type whose
    pretty-printer follows the same convention.

    Raw C-string pointer types do not receive ``displayhint = "string"``.
    They may still appear expandable, but their full string value is already
    shown inline, so skipping expansion remains harmless.
    """
    return varobj_info.get("displayhint", "") == "string"


def _is_child_of_any(varobj: str, parent_set: set[str]) -> bool:
    """Return True if *varobj* is a GDB child of any varobj in *parent_set*."""
    for parent in parent_set:
        if varobj.startswith(f"{parent}."):
            return True

    return False


def _is_collection_displayhint(displayhint: str) -> bool:
    """Return True for displayhints that should honor expandchildlimit."""
    return displayhint in ("array", "map")
