"""
Shared helpers and type aliases for the local-variable pane modules.
"""

import logging
from dataclasses import dataclass
from typing import TypeAlias

from textual.widgets.tree import TreeNode

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
FrameKey: TypeAlias = tuple[str, str, int, frozenset[BindingKey]] | None


@dataclass(eq=False)
class Binding:
    """State of one top-level row of the Locals tree.

    Every root row belongs to exactly one ``Binding``, keyed by its
    ``BindingKey`` ``(name, address)`` so shadowed same-named variables stay
    distinct.  Only the helpers in ``LocalVariablePaneSupportMixin`` create,
    change, or remove bindings.

    ``varobj`` is the GDB root varobj backing the row.  An empty ``varobj``
    makes the row a *placeholder*: uninitialised memory, a failed
    ``-var-create``, an anonymous-namespace variable that cannot be created
    by name yet, or a shadowed row that could not be re-pinned.

    ``pinned`` means the varobj was created from ``*(type*)address``, so it
    is independent of the selected frame and of scope.  ``type`` is the
    varobj's type, needed to re-pin the row when it becomes shadowed.
    """

    key: BindingKey
    node: TreeNode
    varobj: str = ""
    pinned: bool = False
    type: str = ""


    @property
    def is_placeholder(self) -> bool:
        return not self.varobj
