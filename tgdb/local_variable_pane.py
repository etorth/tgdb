"""
Local variables pane widget — tree view with lazy varobj expansion.

Uses GDB's ``-var-create`` / ``-var-list-children`` / ``-var-update`` MI
commands to build a structured, expandable tree of local variables and their
members.

When GDB stops in the same frame (e.g. after a ``next`` command), the pane
calls ``-var-update --all-values *`` and patches only the changed node labels
in-place, preserving the user's expanded/collapsed state.  A full rebuild is
done only when the set of top-level variable names changes (new frame, or
entering/leaving a function).
"""

from __future__ import annotations

import asyncio
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .gdb_controller import LocalVariable
from .highlight_groups import HighlightGroups
from .pane_base import PaneBase


class LocalVariablePane(PaneBase):
    """Render the current frame's local variables as an expandable tree.

    Top-level variables are created via ``-var-create``.  When the user
    expands a node, ``-var-list-children`` is called lazily to populate
    its children.  After a step command, ``-var-update`` refreshes only the
    changed values without collapsing the tree.
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
        super().__init__(hl, **kwargs)
        self._variables: list[LocalVariable] = []
        # Callbacks set by the app to interact with the GDB controller
        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        self._var_update: Optional[Callable[..., Coroutine]] = None
        # Track created varobj names for cleanup
        self._varobj_names: list[str] = []
        # varobj name → TreeNode for in-place value updates
        self._varobj_to_node: dict[str, TreeNode] = {}
        # Generation counter — cancels stale async tasks
        self._rebuild_gen: int = 0
        # Saved expansion state keyed by frozenset(variable_names).
        # Allows restoring expanded nodes when returning to a previous frame.
        self._saved_expansions: dict[frozenset, set[tuple[str, ...]]] = {}

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
    ) -> None:
        """Set the async callbacks for varobj operations."""
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete
        self._var_update = var_update

    def set_variables(self, variables: list[LocalVariable]) -> None:
        """Called when GDB stops — update in-place or rebuild as needed.

        If the set of top-level variable names is unchanged AND we already
        have varobjs from a previous stop, use ``-var-update`` to refresh
        only the changed values without collapsing the tree.

        Otherwise (new frame, different locals) do a full rebuild.

        An empty *variables* list means the inferior is running (GDB sends
        ``on_locals([])`` with every ``*running`` notification).  We cancel
        any pending rebuild but keep ``_variables`` intact so the next stop
        in the same frame can do an in-place update instead of a full rebuild.
        """
        if not variables:
            # GDB is running — cancel pending tasks but don't touch the tree
            # or _variables; the next stop will compare against the current set.
            self._rebuild_gen += 1
            return

        old_names = {v.name for v in self._variables}
        new_names = {v.name for v in variables}
        self._variables = list(variables)
        self._rebuild_gen += 1
        gen = self._rebuild_gen

        if old_names == new_names and self._varobj_names and self._var_update:
            asyncio.create_task(self._update_tree(gen))
        else:
            # Save the current expansion state before the rebuild wipes the tree.
            if old_names:
                paths = self._collect_expanded_paths()
                if paths:
                    self._saved_expansions[frozenset(old_names)] = paths
            # If we have saved state for the incoming frame, restore it.
            restore = self._saved_expansions.get(frozenset(new_names), set())
            asyncio.create_task(self._rebuild_tree(gen, restore))

    # ------------------------------------------------------------------
    # In-place update (same frame, same variables)
    # ------------------------------------------------------------------

    async def _update_tree(self, gen: int) -> None:
        """Refresh changed values using -var-update, preserving expand state."""
        try:
            changelist = await self._var_update("*")
        except Exception:
            await self._rebuild_tree(gen)
            return

        if self._rebuild_gen != gen:
            return

        for change in changelist:
            varobj_name = change.get("name", "")
            in_scope = change.get("in_scope", "true")
            type_changed = change.get("type_changed", "false") == "true"

            if in_scope != "true" or type_changed:
                # Variable went out of scope or changed type — full rebuild.
                await self._rebuild_tree(gen)
                return

            node = self._varobj_to_node.get(varobj_name)
            if node is None:
                continue

            data = node.data
            if not isinstance(data, dict):
                continue

            new_value = change.get("value", "")
            exp = data.get("exp", "")
            has_children = data.get("has_children", False)

            # Reformat label with the new value.
            if has_children:
                label = exp
                if new_value:
                    label += f" = {self._truncate(new_value)}"
            else:
                label = f"{exp} = {new_value}" if new_value else exp
            node.set_label(label)

            # If the number of children changed, the cached children are stale.
            new_num_children = change.get("new_num_children")
            if new_num_children is not None and data.get("loaded"):
                data["loaded"] = False
                node.remove_children()
                node.add_leaf("⏳ loading...")

    # ------------------------------------------------------------------
    # Expansion save / restore helpers
    # ------------------------------------------------------------------

    def _collect_expanded_paths(self) -> set[tuple[str, ...]]:
        """Walk the live tree and return the set of expanded node paths.

        Each path is a tuple of ``exp`` (display-name) strings from the
        root down to the expanded node, e.g. ``("w", "test")``.
        Only expanded nodes are recorded; non-expanded subtrees are skipped.
        """
        try:
            tree = self.query_one(Tree)
        except Exception:
            return set()

        paths: set[tuple[str, ...]] = set()

        def walk(node: TreeNode, path: tuple[str, ...]) -> None:
            for child in node.children:
                data = child.data
                if not isinstance(data, dict):
                    continue
                exp = data.get("exp", "")
                if not exp:
                    continue
                child_path = path + (exp,)
                if child.is_expanded:
                    paths.add(child_path)
                    walk(child, child_path)  # only recurse into expanded nodes

        walk(tree.root, ())
        return paths

    async def _ensure_children_loaded(self, node: TreeNode) -> bool:
        """Load *node*'s children from GDB if they haven't been fetched yet.

        Returns ``True`` when children are available (either already loaded
        or just loaded now), ``False`` when the node has no children or the
        load fails.
        """
        data = node.data
        if not isinstance(data, dict) or not data.get("has_children"):
            return False
        if data.get("loaded"):
            return True
        varobj = data.get("varobj", "")
        if not varobj or not self._var_list_children:
            return False
        data["loaded"] = True
        node.remove_children()
        try:
            children = await self._var_list_children(varobj)
            if children:
                await self._add_children(node, children)
            else:
                node.add_leaf("(empty)")
            return bool(children)
        except Exception:
            data["loaded"] = False
            node.add_leaf("⚠ error fetching children")
            return False

    async def _restore_expansion(
        self, node: TreeNode, path: tuple[str, ...], gen: int
    ) -> None:
        """Expand the node at *path* (relative to *node*), loading children
        on demand at each level so the full path becomes visible.

        ``path`` is a tuple of ``exp`` names, e.g. ``("w", "test")`` expands
        the child named ``w`` and then the grandchild named ``test``.
        """
        if self._rebuild_gen != gen or not path:
            return
        target_exp, rest = path[0], path[1:]
        for child in node.children:
            data = child.data
            if not isinstance(data, dict) or data.get("exp") != target_exp:
                continue
            if not await self._ensure_children_loaded(child):
                break
            if self._rebuild_gen != gen:
                return
            child.expand()
            if rest:
                await self._restore_expansion(child, rest, gen)
            break

    # ------------------------------------------------------------------
    # Full rebuild (new frame / different variable set)
    # ------------------------------------------------------------------

    async def _cleanup_varobjs(self) -> None:
        """Delete all existing varobjs from GDB."""
        if self._var_delete is None:
            return
        for name in self._varobj_names:
            try:
                await self._var_delete(name)
            except Exception:
                pass
        self._varobj_names = []

    async def _rebuild_tree(
        self,
        gen: int,
        restore_paths: "set[tuple[str, ...]] | None" = None,
    ) -> None:
        """Clear the tree and create varobjs for each local variable.

        After building the new tree, re-expand any nodes whose paths are
        listed in *restore_paths* (saved from a previous visit to this frame).
        """
        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        await self._cleanup_varobjs()
        self._varobj_to_node.clear()
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
                node = tree.root.add(
                    label,
                    expand=False,
                    data={"varobj": varobj_name, "exp": var.name, "loaded": False, "has_children": True},
                )
                node.add_leaf("⏳ loading...")
            else:
                label = f"{var.name} = {value}"
                node = tree.root.add_leaf(
                    label,
                    data={"varobj": varobj_name, "exp": var.name, "has_children": False},
                )

            if varobj_name:
                self._varobj_to_node[varobj_name] = node

        # Restore expansion state from a previous visit to this frame.
        # Process shortest paths first so parents are expanded before children.
        if restore_paths:
            for path in sorted(restore_paths, key=len):
                if self._rebuild_gen != gen:
                    return
                await self._restore_expansion(tree.root, path, gen)

    # ------------------------------------------------------------------
    # Lazy child loading
    # ------------------------------------------------------------------

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
        node.remove_children()

        try:
            children = await self._var_list_children(varobj_name)
        except Exception:
            node.add_leaf("⚠ error fetching children")
            return

        if not children:
            node.add_leaf("(empty)")
            return

        await self._add_children(node, children)

    async def _add_children(self, node: TreeNode, children: list[dict]) -> None:
        """Add child nodes, flattening access-specifiers and pairing map entries.

        Also registers every new node in ``_varobj_to_node`` so that
        ``-var-update`` can patch values at any depth without a rebuild.
        """
        _ACCESS = {"public", "private", "protected"}

        paired = self._detect_map_pairs(children)

        if paired:
            for key_child, val_child in paired:
                key_val = key_child.get("value", "?")
                val_name = val_child.get("name", "")
                val_numchild = self._safe_int(val_child.get("numchild", "0"))
                val_dynamic = val_child.get("dynamic", "0") == "1"
                val_has_children = val_numchild > 0 or val_dynamic
                val_value = val_child.get("value", "")
                exp = f"[{key_val}]"

                if val_has_children:
                    label = exp
                    if val_value:
                        label += f" = {self._truncate(val_value)}"
                    child_node = node.add(
                        label,
                        expand=False,
                        data={"varobj": val_name, "exp": exp, "loaded": False, "has_children": True},
                    )
                    child_node.add_leaf("⏳ loading...")
                else:
                    label = f"{exp} = {val_value}" if val_value else exp
                    child_node = node.add_leaf(
                        label,
                        data={"varobj": val_name, "exp": exp, "has_children": False},
                    )

                if val_name:
                    self._varobj_to_node[val_name] = child_node
            return

        for child in children:
            child_name = child.get("name", "")
            exp = child.get("exp", "")
            numchild = self._safe_int(child.get("numchild", "0"))
            dynamic = child.get("dynamic", "0") == "1"
            has_children = numchild > 0 or dynamic
            value = child.get("value", "")

            # Access-specifier pseudo-nodes — expand inline
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
                child_node = node.add(
                    label,
                    expand=False,
                    data={"varobj": child_name, "exp": exp, "loaded": False, "has_children": True},
                )
                child_node.add_leaf("⏳ loading...")
            else:
                label = f"{exp} = {value}" if value else exp
                child_node = node.add_leaf(
                    label,
                    data={"varobj": child_name, "exp": exp, "has_children": False},
                )

            if child_name:
                self._varobj_to_node[child_name] = child_node

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_map_pairs(
        children: list[dict],
    ) -> list[tuple[dict, dict]] | None:
        """Detect alternating key/value pattern from map pretty-printer output.

        Returns ``(key, value)`` pairs if the pattern matches, else ``None``.
        """
        n = len(children)
        if n == 0 or n % 2 != 0:
            return None
        for i, c in enumerate(children):
            if c.get("exp", "") != str(i):
                return None
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
            return s[: max_len - 1] + "…"
        return s
