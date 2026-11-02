"""
Shared helpers and type aliases for the local-variable pane modules.
"""

import enum
import logging
from dataclasses import dataclass
from typing import TypeAlias

from textual.widgets.tree import TreeNode

from ..gdb_controller import LocalVariable
# Re-exported for the sibling modules (``import X as X`` marks a re-export).
from ..varobj_tree.tree import _is_child_of_any as _is_child_of_any
from ..varobj_tree.tree import _suppress_children as _suppress_children

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
# (function, source path, frame level, thread id, binding keys)
FrameKey: TypeAlias = tuple[str, str, int, str, frozenset[BindingKey]] | None


class BindingState(enum.Enum):
    """How a Locals row is backed by GDB."""

    # Varobj created from ``*(type*)address``: valid in any frame and scope.
    PINNED = "pinned"
    # Varobj created by name: bound to the frame selected at creation, so it
    # must be re-created after a frame / thread switch or re-pinned when
    # another variable shadows it.
    NAME_BOUND = "name-bound"
    # No varobj: uninitialised memory, failed creation, an anonymous-namespace
    # variable that cannot be created by name yet, or a shadowed row that
    # could not be re-pinned.
    PLACEHOLDER = "placeholder"


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
    def state(self) -> BindingState:
        """Derived from ``varobj`` / ``pinned`` so it can never disagree with them."""
        if not self.varobj:
            return BindingState.PLACEHOLDER
        if self.pinned:
            return BindingState.PINNED
        return BindingState.NAME_BOUND


    @property
    def is_placeholder(self) -> bool:
        return not self.varobj
