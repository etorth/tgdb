"""
Local variables pane widget — tree view with lazy varobj expansion.

Uses GDB's ``-var-create`` / ``-var-list-children`` / ``-var-update`` /
``-data-evaluate-expression`` MI commands to maintain a structured,
expandable tree of local variables and their members.

Variable identity is based on the stack address of each variable. This lets
tgdb preserve expansion state and update values incrementally instead of
rebuilding the entire tree on every stop.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .config_types import Config
from .gdb_types import Frame, LocalVariable
from .highlight_groups import HighlightGroups
from .local_variable_pane_reconcile import LocalVariablePaneReconcileMixin
from .local_variable_pane_support import LocalVariablePaneSupportMixin
from .local_variable_pane_tree import LocalVariablePaneTreeMixin
from .local_variable_pane_update import LocalVariablePaneUpdateMixin
from .pane_base import PaneBase

__all__ = ["LocalVariablePane"]


class LocalVariablePane(
    LocalVariablePaneReconcileMixin,
    LocalVariablePaneUpdateMixin,
    LocalVariablePaneTreeMixin,
    LocalVariablePaneSupportMixin,
    PaneBase,
):
    """Render the current frame's local variables as an expandable tree."""

    DEFAULT_CSS = """
    LocalVariablePane {
        width: 1fr;
        height: 1fr;
        min-width: 4;
        min-height: 2;
        overflow: hidden;
    }
    LocalVariablePane > Tree {
        width: 1fr;
        height: 1fr;
        background: $surface;
    }
    """

    _RE_CONTAINER_LENGTH = re.compile(r"(?:length|size)\s+(\d+)|with\s+(\d+)\s+elements", re.IGNORECASE)
    _SAFE_CHILD_COUNT = 1_000_000

    def __init__(self, hl: HighlightGroups, cfg: Config, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._cfg = cfg
        self._variables: list[LocalVariable] = []

        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        self._var_update: Optional[Callable[..., Coroutine]] = None
        self._var_eval: Optional[Callable[..., Coroutine]] = None
        self._var_eval_expr: Optional[Callable[..., Coroutine]] = None
        self._get_decl_lines: Optional[Callable[..., Coroutine]] = None

        self._tracked: dict[tuple[str, str], str] = {}
        self._pinned_varobjs: set[str] = set()
        self._varobj_type: dict[str, str] = {}
        self._varobj_to_node: dict[str, TreeNode] = {}
        self._varobj_names: list[str] = []
        self._dynamic_varobjs: set[str] = set()
        self._uninitialized_nodes: dict[tuple[str, str], TreeNode] = {}

        self._frame_key: tuple | None = None
        self._saved_expansions: dict[tuple, set[tuple[tuple[str, int], ...]]] = {}
        self._rebuild_gen = 0


    def title(self) -> str:
        return "LOCALS"


    def compose(self):
        yield from super().compose()
        yield Tree("", id="var-tree")


    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        tree.root.expand()


    def set_var_callbacks(
        self,
        var_create: Callable[..., Coroutine],
        var_list_children: Callable[..., Coroutine],
        var_delete: Callable[..., Coroutine],
        var_update: Callable[..., Coroutine],
        var_eval: Callable[..., Coroutine],
        var_eval_expr: Callable[..., Coroutine],
        get_decl_lines: Callable[..., Coroutine],
    ) -> None:
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete
        self._var_update = var_update
        self._var_eval = var_eval
        self._var_eval_expr = var_eval_expr
        self._get_decl_lines = get_decl_lines


    def set_variables(self, variables: list[LocalVariable], frame: Frame | None = None) -> None:
        """Refresh the pane for the current stop location.

        ``variables=[]`` with ``frame is None`` means the inferior is running,
        so the current tree stays visible until the next stop. ``variables=[]``
        with a real frame means the inferior stopped in a frame with no locals,
        so the pane should update to that empty state.
        """
        self._variables = list(variables)
        self._rebuild_gen += 1
        gen = self._rebuild_gen
        if not variables and frame is None:
            return

        asyncio.create_task(self._update_variables(gen, frame, self._variables))


    @classmethod
    def _parse_container_length(cls, value_str: str) -> int | None:
        """Return the container length from a GDB summary string, or None."""
        if "<error reading" in value_str or "Cannot access memory" in value_str:
            return None

        match = cls._RE_CONTAINER_LENGTH.search(value_str)
        if not match:
            return None

        if match.group(1) is not None:
            return int(match.group(1))

        return int(match.group(2))


    def _child_fetch_limit(self, displayhint: str) -> int:
        """Return the raw GDB child limit for the given pretty-printer hint."""
        limit = self._cfg.expandchildlimit
        if displayhint == "map" and limit > 0:
            return limit * 2

        return limit


    @staticmethod
    def _child_display_count(raw_count: int, displayhint: str) -> int:
        """Convert a raw GDB child count to the user-visible item count."""
        if displayhint == "map":
            return raw_count // 2

        return raw_count
