"""
General-purpose varobj-tree support helpers.
"""

from __future__ import annotations

from textual.widgets.tree import TreeNode


class VarobjTreeSupportMixin:
    """General node/varobj tracking helpers shared by all varobj-tree panes."""

    def _remember_child_varobj(self, child_info: dict, node: TreeNode) -> None:
        varobj_name = child_info.get("name", "")
        if not varobj_name:
            return

        self._varobj_to_node[varobj_name] = node

        if child_info.get("dynamic", "0") == "1":
            self._dynamic_varobjs.add(varobj_name)


    def _build_value_label(self, exp: str, value: str, has_children: bool, collapse_compound: bool = False) -> str:
        if not value:
            return exp

        shown_value = value
        if has_children:
            if collapse_compound:
                shown_value = self._compact_value(value)
            else:
                shown_value = self._truncate(value)

        return f"{exp} = {shown_value}"


    def _add_value_node(
        self,
        parent: TreeNode,
        exp: str,
        value: str,
        has_children: bool,
        *,
        varobj_name: str = "",
        displayhint: str = "",
        prefix: str = "",
        collapse_compound: bool = False,
    ) -> TreeNode:
        label = self._build_value_label(exp, value, has_children, collapse_compound)
        if prefix:
            label = f"{prefix}{label}"

        data = {
            "varobj": varobj_name,
            "exp": exp,
            "has_children": has_children,
            "displayhint": displayhint,
            "prefix": prefix,
        }

        if has_children:
            data["loaded"] = False
            node = parent.add(label, expand=False, data=data)
            node.add_leaf("⏳ loading...")
            return node

        return parent.add_leaf(label, data=data)


    def _collapse_to_leaf_node(
        self,
        node: TreeNode,
        exp: str,
        value: str,
        *,
        prefix: str = "",
        compact_value: bool = False,
    ) -> None:
        if compact_value:
            value = self._compact_value(value)

        label = self._build_value_label(exp, value, False)
        if prefix:
            label = f"{prefix}{label}"

        node.collapse()
        node.remove_children()
        node.allow_expand = False
        node.set_label(label)

        data = node.data
        if not isinstance(data, dict):
            return

        data["varobj"] = ""
        data["has_children"] = False
        data["loaded"] = False
        data["displayhint"] = ""
        data["prefix"] = prefix


    def _remove_varobj_names(self, varobj_name: str, include_root: bool) -> None:
        prefix = f"{varobj_name}."
        stale_names: list[str] = []

        for name in self._varobj_to_node:
            if include_root and name == varobj_name:
                stale_names.append(name)
                continue

            if name.startswith(prefix):
                stale_names.append(name)

        for name in stale_names:
            self._varobj_to_node.pop(name, None)
            self._dynamic_varobjs.discard(name)
            self._varobj_type.pop(name, None)


    def _remove_descendant_varobjs(self, varobj_name: str) -> None:
        self._remove_varobj_names(varobj_name, include_root=False)


    def _purge_varobj_subtree(self, varobj_name: str) -> None:
        """Remove *varobj_name* and its children from tracking dicts."""
        self._remove_varobj_names(varobj_name, include_root=True)


    @staticmethod
    def _safe_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


    @staticmethod
    def _truncate(value: str, max_len: int = 60) -> str:
        value = value.replace("\n", " ")
        if len(value) > max_len:
            return f"{value[: max_len - 1]}…"

        return value


    @staticmethod
    def _compact_value(value: str) -> str:
        if value.strip().startswith("{"):
            return "{...}"

        return value
