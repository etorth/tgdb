"""
Tree expansion, loading, and rendering helpers shared by varobj-tree panes.
"""

import logging

from textual.css.query import NoMatches
from textual.widgets import Tree
from textual.widgets.tree import TreeNode



_log = logging.getLogger("tgdb.varobj_tree")

_ACCESS_SPECIFIERS = {"public", "private", "protected"}


def _suppress_children(varobj_info: dict) -> bool:
    """Return True when the varobj should be shown as a non-expandable leaf.

    GDB's pretty-printer framework sets ``displayhint = "string"`` for every
    string-like type whose printer returns ``display_hint() = 'string'``,
    including ``std::string`` / ``std::wstring`` / ``std::u8string`` /
    ``std::u16string`` / ``std::u32string`` and any future string type whose
    pretty-printer follows the same convention.

    Raw C-string pointer types do not receive ``displayhint = "string"``.
    They may still appear expandable, but their full string value is already
    shown inline, so skipping expansion remains harmless.
    """
    return varobj_info.get("displayhint", "") == "string"


def _is_child_of_any(varobj: str, parent_set: set[str]) -> bool:
    """Return True if *varobj* is a GDB child of any varobj in *parent_set*."""
    for parent in parent_set:
        if varobj.startswith(f"{parent}."):
            return True

    return False


def _is_collection_displayhint(displayhint: str) -> bool:
    """Return True for displayhints that should honor expandchildlimit."""
    return displayhint in ("array", "map")


class VarobjTreeMixin:
    """Tree-focused helpers for varobj-tree panes."""

    def _collect_expanded_paths(self) -> set[tuple[tuple[str, int], ...]]:
        """Return paths for every expanded node in the current tree."""
        try:
            tree = self.query_one(Tree)
        except NoMatches:
            return set()

        paths: set[tuple[tuple[str, int], ...]] = set()

        def walk(node: TreeNode, path: tuple[tuple[str, int], ...]) -> None:
            exp_counts: dict[str, int] = {}

            for child in node.children:
                data = child.data
                if not isinstance(data, dict):
                    continue

                exp = data.get("exp", "")
                if not exp:
                    continue

                child_index = exp_counts.get(exp, 0)
                exp_counts[exp] = child_index + 1
                child_path = path + ((exp, child_index),)

                if not child.is_expanded:
                    continue

                paths.add(child_path)
                walk(child, child_path)

        walk(tree.root, ())
        return paths


    async def _ensure_children_loaded(self, node: TreeNode) -> bool:
        """Load a node's children on demand if they are not loaded yet."""
        data = node.data
        if not isinstance(data, dict):
            return False

        if not data.get("has_children"):
            return False

        status = data.get("load_status", "idle")
        # ``loading`` is treated the same as ``loaded`` for the re-entry
        # guard: another coroutine is already fetching, don't queue a
        # second fetch.  Returning False (vs True for loaded) keeps the
        # caller's "do we have children yet" semantics correct.
        if status == "loaded":
            return True
        if status == "loading":
            return False

        varobj_name = data.get("varobj", "")
        if not varobj_name or not self._var_list_children:
            return False

        data["load_status"] = "loading"
        node.remove_children()
        displayhint = data.get("displayhint", "")

        try:
            children, has_more = await self._var_list_children(varobj_name, limit=self._child_fetch_limit(displayhint))
        except Exception as exc:
            data["load_status"] = "idle"
            _log.warning(f"failed to load children for {varobj_name}: {exc}")
            node.add_leaf(f"⚠ {exc}")
            return False

        if not children:
            data["load_status"] = "loaded"
            node.add_leaf("(empty)")
            return False

        await self._add_children(node, children, displayhint)
        if has_more:
            self._add_load_more_node(node, varobj_name, len(children), displayhint)

        data["load_status"] = "loaded"
        return True


    async def _restore_expansion(self, node: TreeNode, path: tuple[tuple[str, int], ...], gen: int) -> None:
        """Expand the node at *path*, loading intermediate levels on demand."""
        if self._rebuild_gen != gen or not path:
            return

        (target_exp, target_index), rest = path[0], path[1:]
        seen = 0

        for child in node.children:
            data = child.data
            if not isinstance(data, dict):
                continue

            if data.get("exp") != target_exp:
                continue

            if seen != target_index:
                seen += 1
                continue

            if not await self._ensure_children_loaded(child):
                break

            if self._rebuild_gen != gen:
                return

            child.expand()
            if rest:
                await self._restore_expansion(child, rest, gen)
            break


    async def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data
        if not isinstance(data, dict):
            return

        status = data.get("load_status", "idle")
        if status in ("loading", "loaded"):
            return

        data["load_status"] = "loading"
        if data.get("load_more"):
            varobj_name = data.get("varobj", "")
            from_idx = data.get("from_idx", 0)
            parent_displayhint = data.get("displayhint", "")
            if varobj_name and self._var_list_children:
                await self._load_more_children(node, varobj_name, from_idx, parent_displayhint)
                data["load_status"] = "loaded"
                return

            data["load_status"] = "idle"
            return

        varobj_name = data.get("varobj", "")
        if not varobj_name or not self._var_list_children:
            data["load_status"] = "idle"
            return

        await self._load_children(node, varobj_name)
        # _load_children handles its own error transitions; ensure we
        # mark loaded on the success path.  If _load_children set
        # ``load_status = "idle"`` due to an exception, leave it alone.
        if data.get("load_status") == "loading":
            data["load_status"] = "loaded"


    def _get_node_at_screen(self, screen_x: int, screen_y: int) -> TreeNode | None:
        """Return the tree node whose row contains the given screen point."""
        try:
            tree = self.query_one(Tree)
        except NoMatches:
            return None

        tree_region = tree.region
        line_no = screen_y - tree_region.y + int(tree.scroll_offset.y)
        if line_no < 0:
            return None

        return tree.get_node_at_line(line_no)


    async def _expand_node_unlimited(self, node: TreeNode, varobj_name: str, displayhint: str) -> None:
        self._remove_descendant_varobjs(varobj_name)
        node.remove_children()

        try:
            children, _ = await self._var_list_children(varobj_name, limit=0)
        except Exception as exc:
            data = node.data
            if isinstance(data, dict):
                data["load_status"] = "idle"
            node.add_leaf(f"⚠ {exc}")
            node.expand()
            return

        if not children:
            data = node.data
            if isinstance(data, dict):
                data["load_status"] = "loaded"
            node.add_leaf("(empty)")
            node.expand()
            return

        await self._add_children(node, children, displayhint, flat_limit=0)
        data = node.data
        if isinstance(data, dict):
            data["load_status"] = "loaded"
        node.expand()


    def _iter_expandable_child_nodes(self, node: TreeNode) -> list[TreeNode]:
        expandable_children: list[TreeNode] = []

        for child_node in node.children:
            child_data = child_node.data
            if not isinstance(child_data, dict):
                continue

            if not child_data.get("has_children"):
                continue

            if child_data.get("load_more"):
                continue

            expandable_children.append(child_node)

        return expandable_children


    async def _do_expand_recursive(
        self, node: TreeNode, *, limited: bool, _depth: int = 0,
    ) -> None:
        """Shared implementation for do_expand_limited / do_expand_full.

        When *limited* is True, array/map-like nodes honour
        ``cfg.expandchildlimit`` (one "load more" sentinel below the
        first batch); other compound nodes still load all children.
        When *limited* is False, every compound node loads all of
        its children unconditionally.
        """
        if _depth > 20:
            return

        data = node.data
        if not isinstance(data, dict):
            return

        varobj_name = data.get("varobj", "")
        if not varobj_name or not self._var_list_children:
            return

        displayhint = data.get("displayhint", "")
        data["load_status"] = "loading"

        if limited and _is_collection_displayhint(displayhint):
            await self._load_children(node, varobj_name)
            node.expand()
        else:
            await self._expand_node_unlimited(node, varobj_name, displayhint)

        # ``_load_children`` / ``_expand_node_unlimited`` already
        # transition load_status to ``"loaded"`` (success) or
        # ``"idle"`` (error); only stamp ``"loaded"`` here if neither
        # side did so the flag never gets stuck on ``"loading"``.
        if data.get("load_status") == "loading":
            data["load_status"] = "loaded"

        for child_node in self._iter_expandable_child_nodes(node):
            await self._do_expand_recursive(
                child_node, limited=limited, _depth=_depth + 1,
            )


    async def do_expand_limited(self, node: TreeNode, _depth: int = 0) -> None:
        """Recursively expand a subtree using the pane's "expand some" policy.

        This is a public helper used by the locals-pane context menu.

        Behavior:
        - array/map-like nodes honor ``cfg.expandchildlimit``
        - other compound nodes load all of their children
        - recursion continues into every expandable descendant

        The method is safe to call repeatedly. If the pane is not fully wired
        yet (for example callbacks are missing), it becomes a no-op.
        """
        await self._do_expand_recursive(node, limited=True, _depth=_depth)


    async def do_expand_full(self, node: TreeNode, _depth: int = 0) -> None:
        """Recursively expand a subtree without any child-limit paging.

        This is the "load everything" companion to ``do_expand_limited`` and is
        intended for UI actions such as an "Expand All" context-menu command.
        """
        await self._do_expand_recursive(node, limited=False, _depth=_depth)


    def do_fold(self, node: TreeNode) -> None:
        """Recursively collapse a subtree rooted at *node*.

        This only affects the current visible tree state. Frame-to-frame
        expansion persistence is still handled by the pane's reconciliation
        logic the next time ``set_variables`` is called.
        """
        for child_node in list(node.children):
            child_data = child_node.data
            if isinstance(child_data, dict) and child_data.get("has_children"):
                self.do_fold(child_node)

        node.collapse()


    async def _load_children(self, node: TreeNode, varobj_name: str) -> None:
        """Load one batch of children under *node* respecting expandchildlimit."""
        self._remove_descendant_varobjs(varobj_name)
        node.remove_children()

        data = node.data
        if isinstance(data, dict):
            parent_displayhint = data.get("displayhint", "")
        else:
            parent_displayhint = ""

        try:
            children, has_more = await self._var_list_children(varobj_name, limit=self._child_fetch_limit(parent_displayhint))
        except Exception as exc:
            if isinstance(data, dict):
                data["load_status"] = "idle"
            node.add_leaf(f"⚠ {exc}")
            return

        if not children:
            if isinstance(data, dict):
                data["load_status"] = "loaded"
            node.add_leaf("(empty)")
            return

        _log.debug(f"load_children varobj={varobj_name} -> {len(children)} children has_more={has_more}")
        await self._add_children(node, children, parent_displayhint)
        if has_more:
            self._add_load_more_node(node, varobj_name, len(children), parent_displayhint)
        if isinstance(data, dict):
            data["load_status"] = "loaded"


    def _add_load_more_node(self, parent: TreeNode, varobj_name: str, from_idx: int, parent_displayhint: str) -> None:
        """Add a sentinel node that fetches the next child batch on expand."""
        shown = self._child_display_count(from_idx, parent_displayhint)
        if self._cfg.expandchildlimit > 0:
            label = f"load more items [{shown} shown]"
        else:
            label = f"load remaining items [{shown} shown]"

        sentinel = parent.add(
            label,
            expand=False,
            data={
                "load_more": True,
                "load_status": "idle",
                "varobj": varobj_name,
                "from_idx": from_idx,
                "displayhint": parent_displayhint,
            },
        )
        sentinel.add_leaf("")


    async def _load_more_children(self, sentinel: TreeNode, varobj_name: str, from_idx: int, parent_displayhint: str) -> None:
        """Fetch the next child batch and append it after *sentinel*."""
        parent = sentinel.parent
        sentinel.remove()
        if parent is None:
            return

        try:
            children, has_more = await self._var_list_children(
                varobj_name,
                from_idx,
                limit=self._child_fetch_limit(parent_displayhint),
            )
        except Exception as exc:
            parent.add_leaf(f"⚠ {exc}")
            return

        _log.debug(f"load_more_children varobj={varobj_name} from={from_idx} -> {len(children)} children")
        if children:
            await self._add_children(parent, children, parent_displayhint)

        if has_more:
            next_idx = from_idx + len(children)
            self._add_load_more_node(parent, varobj_name, next_idx, parent_displayhint)


    async def _add_children(self, node: TreeNode, children: list[dict], displayhint: str = "", flat_limit: int | None = None) -> None:
        """Add child nodes to *node* using the parent's displayhint."""
        if displayhint == "map":
            await self._add_map_children(node, children)
            return

        await self._add_regular_children(node, children, flat_limit)


    async def _add_map_children(self, node: TreeNode, children: list[dict]) -> None:
        marker_active = True
        node_data = node.data
        if isinstance(node_data, dict):
            marker_active = node_data.get("marker_active", True)

        child_index = 0
        while child_index + 1 < len(children):
            key_child = children[child_index]
            value_child = children[child_index + 1]
            child_index += 2

            value = value_child.get("value", "")
            exp = f"[{key_child.get('value', '?')}]"
            numchild = self._safe_int(value_child.get("numchild", "0"))
            dynamic = value_child.get("dynamic", "0") == "1"
            has_children = (numchild > 0 or dynamic) and not _suppress_children(value_child)
            displayhint = value_child.get("displayhint", "")

            child_node = self._add_value_node(
                node,
                exp,
                value,
                has_children,
                varobj_name=value_child.get("name", ""),
                displayhint=displayhint,
                marker_active=marker_active,
            )
            self._remember_child_varobj(value_child, child_node)


    async def _flatten_access_specifier_children(self, node: TreeNode, varobj_name: str, flat_limit: int | None) -> None:
        if not self._var_list_children:
            return

        child_limit = flat_limit
        if child_limit is None:
            child_limit = self._cfg.expandchildlimit

        try:
            grandchildren, has_more = await self._var_list_children(varobj_name, limit=child_limit)
        except Exception as exc:
            _log.debug(f"Skipping access-specifier children for {varobj_name}: {exc}")
            return

        await self._add_children(node, grandchildren, flat_limit=flat_limit)
        if has_more:
            self._add_load_more_node(node, varobj_name, len(grandchildren), "")


    async def _add_regular_children(self, node: TreeNode, children: list[dict], flat_limit: int | None) -> None:
        marker_active = True
        node_data = node.data
        if isinstance(node_data, dict):
            marker_active = node_data.get("marker_active", True)

        for child in children:
            exp = child.get("exp", "")
            numchild = self._safe_int(child.get("numchild", "0"))
            dynamic = child.get("dynamic", "0") == "1"
            has_children = (numchild > 0 or dynamic) and not _suppress_children(child)

            if exp in _ACCESS_SPECIFIERS and has_children:
                await self._flatten_access_specifier_children(node, child.get("name", ""), flat_limit)
                continue

            child_node = self._add_value_node(
                node,
                exp,
                child.get("value", ""),
                has_children,
                varobj_name=child.get("name", ""),
                displayhint=child.get("displayhint", ""),
                marker_active=marker_active,
            )
            self._remember_child_varobj(child, child_node)
