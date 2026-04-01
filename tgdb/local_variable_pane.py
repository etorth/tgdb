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
        # Generation counter to cancel stale rebuilds
        self._rebuild_gen: int = 0

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
        self._rebuild_gen += 1
        asyncio.create_task(self._rebuild_tree(self._rebuild_gen))

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

    async def _rebuild_tree(self, gen: int) -> None:
        """Clear the tree and create varobjs for each local variable."""
        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        await self._cleanup_varobjs()
        tree.clear()

        if self._rebuild_gen != gen:
            return

        if not self._var_create:
            for var in self._variables:
                val = var.value.replace("\n", " ") if var.value else "<complex>"
                tree.root.add_leaf(f"{var.name} = {val}")
            return

        for var in self._variables:
            if self._rebuild_gen != gen:
                return
            try:
                info = await self._var_create(var.name)
            except Exception:
                val = var.value.replace("\n", " ") if var.value else "<complex>"
                tree.root.add_leaf(f"{var.name} = {val}")
                continue

            if self._rebuild_gen != gen:
                return

            varobj_name = info.get("name", "")
            if varobj_name:
                self._varobj_names.append(varobj_name)

            numchild = self._safe_int(info.get("numchild", "0"))
            has_children = numchild > 0 or info.get("dynamic", "0") == "1"
            value = info.get("value", "")

            if has_children:
                label = var.name
                if value:
                    label += f" = {self._truncate(value)}"
                node = tree.root.add(label, expand=False)
                node.data = {"varobj": varobj_name, "loaded": False}
                node.add_leaf("⏳ loading...")
            else:
                label = f"{var.name} = {value}"
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
        """Add child nodes, transparently flattening access specifier groups.

        For map-like containers whose pretty-printer emits alternating
        key/value children (``[0]``=key, ``[1]``=value, …), pairs them
        into single nodes labeled ``[key] = value``.
        """
        _ACCESS = {"public", "private", "protected"}

        # Detect alternating key/value pattern from map pretty-printer:
        # children come as [0]=key, [1]=value, [2]=key, [3]=value, ...
        # Heuristic: even count, sequential numeric exp labels, odd
        # children all have the same container-like type.
        paired = self._detect_map_pairs(children)

        if paired:
            for key_child, val_child in paired:
                key_val = key_child.get("value", "?")
                val_name = val_child.get("name", "")
                val_numchild = self._safe_int(val_child.get("numchild", "0"))
                val_dynamic = val_child.get("dynamic", "0") == "1"
                val_has_children = val_numchild > 0 or val_dynamic
                val_value = val_child.get("value", "")

                if val_has_children:
                    label = f"[{key_val}]"
                    if val_value:
                        label += f" = {self._truncate(val_value)}"
                    child_node = node.add(label, expand=False)
                    child_node.data = {"varobj": val_name, "loaded": False}
                    child_node.add_leaf("⏳ loading...")
                else:
                    label = f"[{key_val}] = {val_value}" if val_value else f"[{key_val}]"
                    node.add_leaf(label)
            return

        for child in children:
            child_name = child.get("name", "")
            exp = child.get("exp", "")
            numchild = self._safe_int(child.get("numchild", "0"))
            dynamic = child.get("dynamic", "0") == "1"
            has_children = numchild > 0 or dynamic
            value = child.get("value", "")

            # Access specifier pseudo-nodes — expand inline
            if exp in _ACCESS and has_children:
                try:
                    grandchildren = await self._var_list_children(child_name)
                    await self._add_children(node, grandchildren)
                except Exception:
                    pass
                continue

            if has_children:
                label = exp
                if value:
                    label += f" = {self._truncate(value)}"
                child_node = node.add(label, expand=False)
                child_node.data = {"varobj": child_name, "loaded": False}
                child_node.add_leaf("⏳ loading...")
            else:
                label = f"{exp} = {value}" if value else exp
                node.add_leaf(label)

    @staticmethod
    def _detect_map_pairs(
        children: list[dict],
    ) -> list[tuple[dict, dict]] | None:
        """If *children* look like map pretty-printer output (alternating
        key/value with sequential ``[0], [1], [2], [3], …`` labels),
        return a list of ``(key_child, value_child)`` pairs.

        Returns ``None`` if the pattern does not match.
        """
        n = len(children)
        if n == 0 or n % 2 != 0:
            return None
        # Check that exp labels are sequential integers 0..n-1
        for i, c in enumerate(children):
            if c.get("exp", "") != str(i):
                return None
        # Pair them up
        return [(children[i], children[i + 1]) for i in range(0, n, 2)]

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
