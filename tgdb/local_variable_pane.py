"""
Local variables pane widget — tree view with lazy varobj expansion.

Uses GDB's ``-var-create`` / ``-var-list-children`` MI commands to build
a structured, expandable tree of local variables and their members.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Coroutine, Optional

from textual.widget import Widget
from textual.widgets import Tree
from textual.widgets.tree import TreeNode
from rich.text import Text

from .gdb_controller import LocalVariable
from .highlight_groups import HighlightGroups
from .pane_utils import center_cells


class LocalVariablePane(Widget):
    """Render the current frame's local variables as an expandable tree.

    Top-level variables are created via ``-var-create``.  When the user
    expands a node, ``-var-list-children`` is called lazily to populate
    its children.
    """

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

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self.can_focus = True
        self._variables: list[LocalVariable] = []
        # Callbacks set by the app to interact with the GDB controller
        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        # Track created varobj names so we can clean them up
        self._varobj_names: list[str] = []

    def compose(self):
        yield Tree("Local Variables", id="var-tree")

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.root.expand()

    def set_var_callbacks(
        self,
        var_create: Callable[..., Coroutine],
        var_list_children: Callable[..., Coroutine],
        var_delete: Callable[..., Coroutine],
    ) -> None:
        """Set the async callbacks for varobj operations."""
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete

    def set_variables(self, variables: list[LocalVariable]) -> None:
        """Called when the frame changes — rebuild the tree from scratch."""
        self._variables = list(variables)
        asyncio.create_task(self._rebuild_tree())

    async def _cleanup_varobjs(self) -> None:
        """Delete all existing varobjs."""
        if self._var_delete is None:
            return
        for name in self._varobj_names:
            try:
                await self._var_delete(name)
            except Exception:
                pass
        self._varobj_names = []

    async def _rebuild_tree(self) -> None:
        """Clear the tree and create varobjs for each local variable."""
        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        await self._cleanup_varobjs()
        tree.clear()

        if not self._var_create:
            # No GDB connection — fall back to flat display
            for var in self._variables:
                prefix = "[arg] " if var.is_arg else ""
                val = var.value.replace("\n", " ") if var.value else "<complex>"
                label = f"{prefix}{var.name}: {var.type} = {val}"
                tree.root.add_leaf(label)
            return

        for var in self._variables:
            try:
                info = await self._var_create(var.name)
            except Exception:
                # Fall back to flat display for this variable
                prefix = "[arg] " if var.is_arg else ""
                val = var.value.replace("\n", " ") if var.value else "<complex>"
                tree.root.add_leaf(f"{prefix}{var.name} = {val}")
                continue

            varobj_name = info.get("name", "")
            if varobj_name:
                self._varobj_names.append(varobj_name)

            numchild = self._safe_int(info.get("numchild", "0"))
            value = info.get("value", "")
            var_type = info.get("type", var.type)
            prefix = "[arg] " if var.is_arg else ""

            if numchild > 0:
                label = f"{prefix}{var.name}: {var_type}"
                if value:
                    label += f" = {self._truncate(value)}"
                node = tree.root.add(label, expand=False)
                # Store varobj metadata for lazy expansion
                node.data = {"varobj": varobj_name, "loaded": False}
                # Add a placeholder so the node is expandable
                node.add_leaf("⏳ loading...")
            else:
                label = f"{prefix}{var.name}: {var_type} = {value}"
                tree.root.add_leaf(label)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Lazily load children when a node is expanded."""
        node = event.node
        data = node.data
        if not isinstance(data, dict):
            return
        if data.get("loaded"):
            return
        varobj = data.get("varobj", "")
        if not varobj or not self._var_list_children:
            return
        data["loaded"] = True
        asyncio.create_task(self._load_children(node, varobj))

    async def _load_children(self, node: TreeNode, varobj_name: str) -> None:
        """Fetch children from GDB and populate the tree node."""
        # Remove placeholder
        node.remove_children()

        try:
            children = await self._var_list_children(varobj_name)
        except Exception:
            node.add_leaf("⚠ error fetching children")
            return

        if not children:
            node.add_leaf("(empty)")
            return

        # Skip access specifier nodes (public/private/protected) — flatten
        # their children directly into the parent node.
        await self._add_children(node, children)

    async def _add_children(self, node: TreeNode, children: list[dict]) -> None:
        """Add child nodes, transparently flattening access specifier groups."""
        _ACCESS = {"public", "private", "protected"}
        for child in children:
            child_name = child.get("name", "")
            exp = child.get("exp", "")
            numchild = self._safe_int(child.get("numchild", "0"))
            value = child.get("value", "")
            child_type = child.get("type", "")

            # Access specifier pseudo-nodes — expand inline
            if exp in _ACCESS and numchild > 0:
                try:
                    grandchildren = await self._var_list_children(child_name)
                    await self._add_children(node, grandchildren)
                except Exception:
                    pass
                continue

            if numchild > 0:
                label = f"{exp}: {child_type}" if child_type else exp
                if value:
                    label += f" = {self._truncate(value)}"
                child_node = node.add(label, expand=False)
                child_node.data = {"varobj": child_name, "loaded": False}
                child_node.add_leaf("⏳ loading...")
            else:
                if child_type:
                    label = f"{exp}: {child_type} = {value}"
                else:
                    label = f"{exp} = {value}" if value else exp
                node.add_leaf(label)

    @staticmethod
    def _safe_int(val) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _truncate(s: str, max_len: int = 60) -> str:
        s = s.replace("\n", " ")
        if len(s) > max_len:
            return s[:max_len - 1] + "…"
        return s
