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

_TAG_ARG = "\U0001F170\uFE0F "
_TAG_LOCAL = "\U0001F17B\uFE0F "
_TAG_SHADOW = "\U0001F182\uFE0F "

BindingKey: TypeAlias = tuple[str, str]
BindingEntry: TypeAlias = tuple[str, str, LocalVariable]
ExpansionSegment: TypeAlias = tuple[str, int]
ExpansionPath: TypeAlias = tuple[ExpansionSegment, ...]
FrameKey: TypeAlias = tuple[str, str, frozenset[BindingKey]] | None
