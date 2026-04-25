"""
Public implementation of the evaluate-pane package.

``EvaluatePane`` renders each watch expression as an expandable varobj tree
node, matching the tree structure used by ``LocalVariablePane``.  The caller
constructs the pane, injects the varobj callbacks, then mutates the watch list
through the public methods documented on the class below.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree

from ..config import Config
from ..highlight_groups import HighlightGroups
from ..varobj_tree import VarobjTreePane
from ..varobj_tree.shared import _suppress_children


class EvaluatePane(VarobjTreePane):
    """Render a watch-expression list as an expandable varobj tree.

    Public interface
    ----------------
    ``EvaluatePane(hl, cfg=None, **kwargs)``
        Create the widget with an empty watch list.

    ``set_var_callbacks(var_create, var_list_children, var_delete, var_update, var_eval_expr)``
        Inject the async callbacks used to create, expand, delete and update
        varobjs.  Must be called before ``add_expression``.

    ``set_eval_fn(fn)``
        Legacy compatibility shim.  No-op in the tree-based implementation.

    ``add_expression(expr)``
        Append a new watch expression and create its root varobj tree node.

    ``remove_expression(index)``
        Remove one watch expression by 0-based index.

    ``refresh_all()``
        Re-evaluate every watch varobj, typically after the inferior stops.

    ``do_expand_limited(node)``, ``do_expand_full(node)``, ``do_fold(node)``
        Subtree expansion/collapse helpers compatible with the context-menu
        layer (same contract as ``LocalVariablePane``).
    """

    DEFAULT_CSS = """
    EvaluatePane {
        width: 1fr;
        height: 1fr;
        min-width: 4;
        min-height: 2;
        overflow: hidden;
    }
    """

    def __init__(self, hl: HighlightGroups, cfg: Optional[Config] = None, **kwargs) -> None:
        """Create an empty evaluate pane.

        Args:
            hl:  Highlight-group palette shared by tgdb panes.
            cfg: Runtime configuration used for ``expandchildlimit``.  A
                 default ``Config()`` is used when omitted.
        """
        super().__init__(hl, cfg, **kwargs)
        self._expressions: list[str] = []
        self._expr_varobjs: list[str] = []


    def title(self) -> str:
        return "EVALUATIONS"


    def set_eval_fn(self, fn: Callable) -> None:
        """Legacy compatibility shim — no-op in the tree-based implementation."""


    def add_expression(self, expr: str) -> None:
        """Append a watch expression and start creating its varobj tree node."""
        idx = len(self._expressions)
        self._expressions.append(expr)
        self._expr_varobjs.append("")
        asyncio.create_task(self._create_expression_node(idx, expr))


    def remove_expression(self, index: int) -> Optional[str]:
        """Remove one watch expression by 0-based index and return it."""
        if not (0 <= index < len(self._expressions)):
            return None

        removed_expr = self._expressions.pop(index)
        removed_varobj = self._expr_varobjs.pop(index)

        if removed_varobj:
            node = self._varobj_to_node.pop(removed_varobj, None)
            if node is not None:
                node.remove()
            self._purge_varobj_subtree(removed_varobj)
            self._pinned_varobjs.discard(removed_varobj)
            if removed_varobj in self._varobj_names:
                self._varobj_names.remove(removed_varobj)
            if self._var_delete:
                asyncio.create_task(self._delete_varobj_safe(removed_varobj))

        return removed_expr


    async def refresh_all(self, current_frame: Optional[object] = None) -> None:
        """Re-evaluate every watch expression varobj after the inferior stops."""
        if not self._varobj_names or not self._var_update:
            return

        try:
            changelist = await self._var_update("*")
        except Exception:
            return

        self._apply_watch_changelist(changelist)


    async def _create_expression_node(self, idx: int, expr: str) -> None:
        """Create a varobj for *expr* and add it as a root tree node."""
        if not self._var_create:
            return

        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        try:
            info = await self._var_create(expr)
        except Exception:
            if idx < len(self._expressions) and self._expressions[idx] == expr:
                tree.root.add_leaf(
                    f"{expr} = <error>",
                    data={"varobj": "", "exp": expr, "has_children": False, "displayhint": ""},
                )
            return

        if idx >= len(self._expressions) or self._expressions[idx] != expr:
            varobj_name = info.get("name", "")
            if varobj_name and self._var_delete:
                asyncio.create_task(self._delete_varobj_safe(varobj_name))
            return

        varobj_name = info.get("name", "")
        self._expr_varobjs[idx] = varobj_name

        if varobj_name:
            if varobj_name not in self._varobj_names:
                self._varobj_names.append(varobj_name)
            if info.get("dynamic", "0") == "1":
                self._dynamic_varobjs.add(varobj_name)
            type_str = info.get("type", "")
            if type_str:
                self._varobj_type[varobj_name] = type_str

        value = info.get("value", "")
        numchild = self._safe_int(info.get("numchild", "0"))
        has_children = (numchild > 0 or info.get("dynamic", "0") == "1") and not _suppress_children(info)
        displayhint = info.get("displayhint", "")

        node = self._add_value_node(
            tree.root,
            expr,
            value,
            has_children,
            varobj_name=varobj_name,
            displayhint=displayhint,
        )
        if varobj_name:
            self._varobj_to_node[varobj_name] = node


    def _apply_watch_changelist(self, changelist: list[dict]) -> None:
        """Apply ``-var-update`` results to the watch tree."""
        for change in changelist:
            varobj_name = change.get("name", "")
            node = self._varobj_to_node.get(varobj_name)
            if node is None:
                continue

            data = node.data
            if not isinstance(data, dict):
                continue

            if data.get("load_more"):
                continue

            if change.get("in_scope", "true") != "true":
                continue

            if change.get("type_changed", "false") == "true":
                continue

            exp = data.get("exp", "")
            has_children = data.get("has_children", False)
            new_value = change.get("value", "")
            label = self._build_value_label(exp, new_value, has_children)
            node.set_label(label)

            if change.get("new_num_children") is None or not data.get("loaded"):
                continue

            data["loaded"] = False
            node.remove_children()
            if node.is_expanded:
                asyncio.create_task(self._load_children(node, varobj_name))
            else:
                node.add_leaf("⏳ loading...")
