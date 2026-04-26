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

BindingKey: TypeAlias = tuple[str, str]
BindingEntry: TypeAlias = tuple[str, str, LocalVariable]
ExpansionSegment: TypeAlias = tuple[str, int]
ExpansionPath: TypeAlias = tuple[ExpansionSegment, ...]
FrameKey: TypeAlias = tuple[str, str, frozenset[BindingKey]] | None
