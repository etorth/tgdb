"""
Locals-specific support helpers for the local-variable pane.

Binding bookkeeping lives here.  ``_bindings`` maps each top-level row's
``BindingKey`` to its ``Binding``; the helpers below are the only code that
adds, changes, or removes bindings, so the binding table, the varobj
registry (``_varobj_to_node`` / ``_dynamic_varobjs``), and the tree stay
consistent:

- ``_track_binding`` — row backed by a root varobj (new, promoted, re-pinned)
- ``_add_placeholder_node`` — row without a varobj
- ``_demote_to_placeholder`` — keep the row's node, forget its varobj
- ``_drop_binding`` — remove the row (the caller deletes the GDB varobj)
- ``_purge_stale_varobj`` — GDB lost a varobj; forget it and re-create the row
"""

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .shared import Binding, BindingKey, ExpansionPath, _log


class LocalVariablePaneSupportMixin:
    """Locals-specific helpers: binding bookkeeping, shadowing, placeholder nodes."""

    @staticmethod
    def _binding_marker_active(key: BindingKey, shadowed_keys: set[BindingKey]) -> bool:
        return key not in shadowed_keys



    # ------------------------------------------------------------------
    # Binding queries
    # ------------------------------------------------------------------


    def _tracked_bindings(self) -> list[Binding]:
        """Bindings backed by a root varobj, in insertion order."""
        tracked: list[Binding] = []
        for binding in self._bindings.values():
            if not binding.is_placeholder:
                tracked.append(binding)
        return tracked


    def _placeholder_bindings(self) -> list[Binding]:
        placeholders: list[Binding] = []
        for binding in self._bindings.values():
            if binding.is_placeholder:
                placeholders.append(binding)
        return placeholders


    def _tracked_keys(self) -> set[BindingKey]:
        return {binding.key for binding in self._tracked_bindings()}


    def _root_varobjs(self) -> list[str]:
        """Root varobj names of all tracked rows (the ``-var-update`` set)."""
        return [binding.varobj for binding in self._tracked_bindings()]


    def _varobj_of(self, key: BindingKey) -> str:
        """Root varobj of the row *key*, or ``""`` (no row / placeholder)."""
        binding = self._bindings.get(key)
        if binding is None:
            return ""
        return binding.varobj


    def _binding_for_varobj(self, varobj_name: str) -> Binding | None:
        """The row whose root varobj is *varobj_name* (``None`` for children)."""
        if not varobj_name:
            return None
        for binding in self._bindings.values():
            if binding.varobj == varobj_name:
                return binding
        return None



    # ------------------------------------------------------------------
    # Binding transitions
    # ------------------------------------------------------------------


    def _track_binding(
        self,
        key: BindingKey,
        node: TreeNode,
        info: dict,
        *,
        pinned: bool,
        fallback_type: str = "",
    ) -> Binding:
        """Record that row *key* is shown by *node* and backed by varobj ``info["name"]``.

        *info* is the ``-var-create`` payload.  Replaces any previous binding
        for *key*; a previous binding's node, if different from *node*, is
        removed from the tree.
        """
        varobj_name = info.get("name", "")
        previous = self._bindings.get(key)
        if previous is not None:
            if previous.varobj and previous.varobj != varobj_name:
                _log.debug(f"binding {key} replaced while still tracking {previous.varobj}")
                self._purge_varobj_subtree(previous.varobj)
            if previous.node is not node:
                previous.node.remove()

        binding = Binding(
            key=key,
            node=node,
            varobj=varobj_name,
            pinned=pinned and bool(varobj_name),
            type=info.get("type", "") or fallback_type,
        )
        self._bindings[key] = binding

        if varobj_name:
            self._varobj_to_node[varobj_name] = node
            # A recycled varobj name must not inherit a stale dynamic flag;
            # it would route ``_do_var_update`` down the dynamic path.
            self._dynamic_varobjs.discard(varobj_name)
            if info.get("dynamic", "0") == "1":
                self._dynamic_varobjs.add(varobj_name)

        return binding


    def _add_placeholder_node(
        self,
        tree: Tree,
        key: BindingKey,
        exp: str,
        label: str,
        *,
        marker_active: bool = True,
    ) -> TreeNode:
        """Show row *key* as a leaf without a varobj, replacing any previous row."""
        self._drop_binding(key)
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
        self._bindings[key] = Binding(key=key, node=node)
        return node


    def _demote_to_placeholder(self, binding: Binding) -> None:
        """Keep *binding*'s node but forget its varobj.

        The caller has deleted (or is about to delete) the GDB-side varobj.
        """
        if binding.varobj:
            self._purge_varobj_subtree(binding.varobj)
        binding.varobj = ""
        binding.pinned = False


    def _drop_binding(self, key: BindingKey) -> Binding | None:
        """Remove row *key* from the tree and from tracking.

        Returns the removed binding (its ``varobj`` still set) so the caller
        can delete the GDB-side varobj, or ``None`` if there was no row.
        """
        binding = self._bindings.pop(key, None)
        if binding is None:
            return None

        if binding.varobj:
            self._purge_varobj_subtree(binding.varobj)
        binding.node.remove()
        return binding


    def _remove_placeholder_node(self, key: BindingKey) -> None:
        binding = self._bindings.get(key)
        if binding is not None and binding.is_placeholder:
            self._drop_binding(key)


    def _remove_out_of_scope_placeholders(self, live_keys: set[BindingKey]) -> None:
        for binding in self._placeholder_bindings():
            if binding.key not in live_keys:
                self._drop_binding(binding.key)


    def _purge_stale_varobj(self, varobj_name: str) -> None:
        """GDB reported *varobj_name* as "not found": forget it.

        A child only leaves the varobj registry.  A root's row is dropped
        and queued in ``_shape_readd_keys`` so the current reconciliation
        pass re-creates it instead of leaving a frozen row behind.
        """
        binding = self._binding_for_varobj(varobj_name)
        if binding is None:
            self._purge_varobj_subtree(varobj_name)
            return

        self._drop_binding(binding.key)
        self._shape_readd_keys.add(binding.key)



    # ------------------------------------------------------------------
    # Node helpers
    # ------------------------------------------------------------------


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
