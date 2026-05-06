"""
Locals-specific support helpers for the local-variable pane.
"""

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .shared import BindingKey, ExpansionPath


class LocalVariablePaneSupportMixin:
    """Locals-specific helpers: shadowing, placeholder nodes, varobj registration."""

    @staticmethod
    def _binding_marker_active(key: BindingKey, shadowed_keys: set[BindingKey]) -> bool:
        return key not in shadowed_keys


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


    def _add_placeholder_node(
        self,
        tree: Tree,
        key: BindingKey,
        exp: str,
        label: str,
        *,
        marker_active: bool = True,
    ) -> TreeNode:
        self._remove_placeholder_node(key)
        node = tree.root.add_leaf(
            label,
            data={
                "varobj": "",
                "exp": exp,
                "has_children": False,
                "displayhint": "",
                "marker_active": marker_active,
            },
        )
        self._uninitialized_nodes[key] = node
        return node


    def _set_node_marker_active(self, node: TreeNode, marker_active: bool) -> None:
        data = node.data
        if not isinstance(data, dict):
            return

        if data.get("load_more"):
            return

        data["marker_active"] = marker_active
        label = node.label
        if hasattr(label, "copy"):
            label = label.copy()

        node.set_label(label)

        for child in node.children:
            self._set_node_marker_active(child, marker_active)


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

        # Drop any stale dynamic-flag entry first.  In normal operation
        # GDB hands out fresh varobj names so a re-add can't collide with
        # a prior dynamic flag, but if a name ever IS recycled, leaving
        # the old flag in place would cause ``_do_var_update`` to take
        # the dynamic-container code path on a non-dynamic varobj —
        # incorrect updates and possibly skipped values.  Be explicit.
        self._dynamic_varobjs.discard(varobj_name)
        if info.get("dynamic", "0") == "1":
            self._dynamic_varobjs.add(varobj_name)

        type_str = info.get("type", "")
        if not type_str:
            type_str = fallback_type

        if type_str:
            self._varobj_type[varobj_name] = type_str

        return varobj_name
