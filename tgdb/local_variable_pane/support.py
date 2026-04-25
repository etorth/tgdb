"""
Locals-specific support helpers for the local-variable pane.
"""

from __future__ import annotations

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .shared import BindingKey, ExpansionPath, _SHADOW_SUFFIX


class LocalVariablePaneSupportMixin:
    """Locals-specific helpers: shadowing, placeholder nodes, varobj registration."""

    def _shadow_suffix(self, key: BindingKey, shadowed_keys: set[BindingKey]) -> str:
        if key in shadowed_keys:
            return _SHADOW_SUFFIX

        return ""


    def _remove_placeholder_node(self, key: BindingKey) -> None:
        node = self._uninitialized_nodes.pop(key, None)
        if node is None:
            return

        node.remove()


    def _remove_out_of_scope_placeholders(self, live_keys: set[BindingKey]) -> None:
        stale_keys = list(self._uninitialized_nodes.keys())
        for key in stale_keys:
            if key in live_keys:
                continue

            self._remove_placeholder_node(key)


    def _add_placeholder_node(self, tree: Tree, key: BindingKey, exp: str, label: str) -> TreeNode:
        self._remove_placeholder_node(key)
        node = tree.root.add_leaf(
            label,
            data={"varobj": "", "exp": exp, "has_children": False, "displayhint": ""},
        )
        self._uninitialized_nodes[key] = node
        return node


    def _restore_paths_for_name(self, restore: set[ExpansionPath], name: str) -> list[ExpansionPath]:
        matching_paths: list[ExpansionPath] = []

        for path in restore:
            if not path:
                continue

            if path[0][0] != name:
                continue

            matching_paths.append(path)

        matching_paths.sort(key=len)
        return matching_paths


    def _remember_root_varobj(self, key: BindingKey, info: dict, is_pinned: bool = False) -> str:
        varobj_name = info.get("name", "")
        self._tracked[key] = varobj_name
        if not varobj_name:
            return ""

        if varobj_name not in self._varobj_names:
            self._varobj_names.append(varobj_name)

        if is_pinned:
            self._pinned_varobjs.add(varobj_name)

        if info.get("dynamic", "0") == "1":
            self._dynamic_varobjs.add(varobj_name)

        type_str = info.get("type", "")
        if type_str:
            self._varobj_type[varobj_name] = type_str

        return varobj_name


    def _remember_reanchored_varobj(self, key: BindingKey, info: dict, fallback_type: str) -> str:
        varobj_name = info.get("name", "")
        self._tracked[key] = varobj_name
        if not varobj_name:
            return ""

        if varobj_name not in self._varobj_names:
            self._varobj_names.append(varobj_name)

        self._pinned_varobjs.add(varobj_name)

        if info.get("dynamic", "0") == "1":
            self._dynamic_varobjs.add(varobj_name)

        type_str = info.get("type", "")
        if not type_str:
            type_str = fallback_type

        if type_str:
            self._varobj_type[varobj_name] = type_str

        return varobj_name
