"""
Shared helpers and type aliases for the local-variable pane modules.
"""

import logging
from typing import TypeAlias

from ..gdb_controller import LocalVariable
from ..varobj_tree.tree import (
    _ACCESS_SPECIFIERS,
    _is_child_of_any,
    _is_collection_displayhint,
    _suppress_children,
)

_log = logging.getLogger("tgdb.locals")


def _type_needs_name_fallback(type_str: str) -> bool:
    """Return True if *type_str* contains an anonymous namespace qualifier.

    Types like ``(anonymous namespace)::Foo`` cannot be used in cast
    expressions (``*(type*)addr``) because GDB's expression parser rejects
    the parentheses.  These variables must be created by plain name instead.
    """
    return "(anonymous namespace)" in type_str


BindingKey: TypeAlias = tuple[str, str]
BindingEntry: TypeAlias = tuple[str, str, LocalVariable]
ExpansionSegment: TypeAlias = tuple[str, int]
ExpansionPath: TypeAlias = tuple[ExpansionSegment, ...]
FrameKey: TypeAlias = tuple[str, str, frozenset[BindingKey]] | None
