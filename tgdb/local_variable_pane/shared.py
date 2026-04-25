"""
Shared helpers and type aliases for the local-variable pane modules.
"""

from __future__ import annotations

import logging
from typing import TypeAlias

from ..gdb_controller import LocalVariable
from ..varobj_tree.shared import (
    _ACCESS_SPECIFIERS,
    _is_child_of_any,
    _is_collection_displayhint,
    _suppress_children,
)

_log = logging.getLogger("tgdb.locals")

_TAG_ARG = "\U0001F130 "
_TAG_LOCAL = "\U0001F13B "
_TAG_SHADOW = "\U0001F142 "

BindingKey: TypeAlias = tuple[str, str]
BindingEntry: TypeAlias = tuple[str, str, LocalVariable]
ExpansionSegment: TypeAlias = tuple[str, int]
ExpansionPath: TypeAlias = tuple[ExpansionSegment, ...]
FrameKey: TypeAlias = tuple[str, str, frozenset[BindingKey]] | None
