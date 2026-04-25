"""Base class for varobj-tree panes (LocalVariablePane, EvaluatePane)."""

from __future__ import annotations

import asyncio
import re
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ..config import Config
from ..highlight_groups import HighlightGroups
from ..pane_base import PaneBase
from .support import VarobjTreeSupportMixin
from .tree import VarobjTreeMixin


class VarobjTreePane(VarobjTreeMixin, VarobjTreeSupportMixin, PaneBase):
    """Shared base for LocalVariablePane and EvaluatePane.

    Provides:
    - All varobj tracking state (_varobj_to_node, _varobj_names, etc.)
    - set_var_callbacks() dependency injection
    - compose() / on_mount() yielding a Tree widget
    - _parse_container_length, _child_fetch_limit, _child_display_count
    - _delete_varobj_safe
    - DEFAULT_CSS for the embedded Tree
    """

    DEFAULT_CSS = """
    VarobjTreePane > Tree {
        width: 1fr;
        height: 1fr;
        background: $surface;
    }
    """

    _RE_CONTAINER_LENGTH = re.compile(r"(?:length|size)\s+(\d+)|with\s+(\d+)\s+elements", re.IGNORECASE)
    _SAFE_CHILD_COUNT = 1_000_000

    def __init__(self, hl: HighlightGroups, cfg: Optional[Config] = None, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._cfg = cfg if cfg is not None else Config()
        self._varobj_to_node: dict[str, TreeNode] = {}
        self._varobj_names: list[str] = []
        self._dynamic_varobjs: set[str] = set()
        self._varobj_type: dict[str, str] = {}
        self._pinned_varobjs: set[str] = set()
        self._rebuild_gen: int = 0
        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        self._var_update: Optional[Callable[..., Coroutine]] = None
        self._var_eval: Optional[Callable[..., Coroutine]] = None
        self._var_eval_expr: Optional[Callable[..., Coroutine]] = None


    def set_var_callbacks(
        self,
        var_create: Callable[..., Coroutine],
        var_list_children: Callable[..., Coroutine],
        var_delete: Callable[..., Coroutine],
        var_update: Callable[..., Coroutine],
        var_eval_expr: Callable[..., Coroutine],
        *,
        var_eval: Optional[Callable[..., Coroutine]] = None,
    ) -> None:
        """Install the async debugger callbacks."""
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete
        self._var_update = var_update
        self._var_eval = var_eval
        self._var_eval_expr = var_eval_expr


    def compose(self):
        yield from super().compose()
        yield Tree("")


    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        tree.root.expand()


    @classmethod
    def _parse_container_length(cls, value_str: str) -> int | None:
        if "<error reading" in value_str or "Cannot access memory" in value_str:
            return None

        match = cls._RE_CONTAINER_LENGTH.search(value_str)
        if not match:
            return None

        if match.group(1) is not None:
            return int(match.group(1))

        return int(match.group(2))


    def _child_fetch_limit(self, displayhint: str) -> int:
        limit = self._cfg.expandchildlimit
        if displayhint == "map" and limit > 0:
            return limit * 2

        return limit


    @staticmethod
    def _child_display_count(raw_count: int, displayhint: str) -> int:
        if displayhint == "map":
            return raw_count // 2

        return raw_count


    async def _delete_varobj_safe(self, varobj_name: str) -> None:
        try:
            await self._var_delete(varobj_name)
        except Exception:
            pass
