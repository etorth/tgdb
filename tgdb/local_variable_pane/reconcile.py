"""
Reconciliation helpers for the local-variable pane.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Tree

from ..gdb_controller import Frame, LocalVariable
from .shared import BindingEntry, BindingKey, ExpansionPath, _SHADOW_SUFFIX, _log, _suppress_children


class LocalVariablePaneReconcileMixin:
    """Tree reconciliation and label-sync helpers."""

    async def _remove_gone_bindings(self, gen: int, to_remove: list[BindingKey]) -> bool:
        for binding_name, binding_addr in to_remove:
            key = (binding_name, binding_addr)
            varobj_name = self._tracked.pop(key, "")
            node = None
            if varobj_name:
                node = self._varobj_to_node.get(varobj_name)

            if varobj_name:
                self._purge_varobj_subtree(varobj_name)
                self._pinned_varobjs.discard(varobj_name)

            if node is not None:
                node.remove()

            if varobj_name and varobj_name in self._varobj_names:
                self._varobj_names.remove(varobj_name)

            if varobj_name and self._var_delete:
                try:
                    await self._var_delete(varobj_name)
                except Exception:
                    pass

            if self._rebuild_gen != gen:
                return False

        return True


    async def _restore_binding_expansion(self, gen: int, tree: Tree, restore: set[ExpansionPath], name: str) -> bool:
        restore_paths = self._restore_paths_for_name(restore, name)

        for path in restore_paths:
            if self._rebuild_gen != gen:
                return False

            await self._restore_expansion(tree.root, path, gen)

        return True


    async def _add_new_binding(
        self,
        gen: int,
        tree: Tree,
        binding: BindingEntry,
        restore: set[ExpansionPath],
        shadowed_keys: set[BindingKey],
    ) -> bool:
        binding_name, binding_addr, variable = binding
        key = (binding_name, binding_addr)
        self._remove_placeholder_node(key)

        shadow_suffix = self._shadow_suffix(key, shadowed_keys)
        stack_value = variable.value or ""

        if "<error reading variable:" in stack_value:
            label = f"{variable.name} = <not yet initialized>{shadow_suffix}"
            self._add_placeholder_node(tree, key, variable.name, label)
            _log.debug(f"Skipping var_create for {variable.name}: uninitialized memory in bvar.value ({stack_value[:80]!r})")
            return True

        try:
            info = await self._var_create(variable.name)
        except Exception:
            value = variable.value.replace("\n", " ")
            if not value:
                value = "<complex>"

            label = f"{variable.name} = {value}{shadow_suffix}"
            self._add_placeholder_node(tree, key, variable.name, label)
            return True

        if self._rebuild_gen != gen:
            return False

        value = info.get("value", "")
        varobj_name = info.get("name", "")
        if varobj_name and "<error reading variable:" in value:
            if self._var_delete:
                try:
                    await self._var_delete(varobj_name)
                except Exception:
                    pass

            label = f"{variable.name} = <not yet initialized>{shadow_suffix}"
            self._add_placeholder_node(tree, key, variable.name, label)
            _log.debug(f"var_create {variable.name}: uninitialized memory in response ({value[:80]!r}), placeholder added")
            return True

        varobj_name = self._remember_root_varobj(key, info)
        numchild = self._safe_int(info.get("numchild", "0"))
        has_children = (numchild > 0 or info.get("dynamic", "0") == "1") and not _suppress_children(info)
        displayhint = info.get("displayhint", "")

        node = self._add_value_node(
            tree.root,
            variable.name,
            value,
            has_children,
            varobj_name=varobj_name,
            displayhint=displayhint,
            shadow_suffix=shadow_suffix,
        )
        if varobj_name:
            self._varobj_to_node[varobj_name] = node

        return await self._restore_binding_expansion(gen, tree, restore, binding_name)


    async def _add_new_bindings(
        self,
        gen: int,
        tree: Tree,
        to_add: list[BindingEntry],
        restore: set[ExpansionPath],
        shadowed_keys: set[BindingKey],
    ) -> bool:
        if not self._var_create:
            return True

        for binding in to_add:
            if self._rebuild_gen != gen:
                return False

            if not await self._add_new_binding(gen, tree, binding, restore, shadowed_keys):
                return False

        return True


    async def _update_variables(self, gen: int, frame: Frame | None, variables: list[LocalVariable]) -> None:
        """Incrementally reconcile the locals tree with the current frame."""
        if self._rebuild_gen != gen:
            return

        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        addrs = await self._eval_variable_addresses(gen, variables)
        if addrs is None:
            return

        if self._rebuild_gen != gen:
            return

        variables = await self._filter_by_decl_lines(variables, frame)
        if self._rebuild_gen != gen:
            return

        new_bindings, new_binding_keys, shadowed_keys = self._compute_bindings(variables, addrs)
        new_frame_key = self._build_frame_key(frame, new_binding_keys)
        current_keys = set(self._tracked.keys())

        self._remove_out_of_scope_placeholders(new_binding_keys)

        to_remove = self._build_removed_bindings(current_keys, new_binding_keys)
        to_add = self._build_added_bindings(current_keys, new_bindings)
        to_reanchor = self._build_reanchor_bindings(current_keys, new_bindings, shadowed_keys)

        no_change = not to_remove and not to_add and not to_reanchor and new_frame_key == self._frame_key
        if no_change:
            changelist = await self._update_unchanged_varobjs(gen, set())
            if changelist or self._rebuild_gen == gen:
                self._sync_shadow_labels(shadowed_keys)
            return

        if self._frame_key is not None and self._frame_key != new_frame_key:
            self._saved_expansions[self._frame_key] = self._collect_expanded_paths()

        self._frame_key = new_frame_key

        stale_varobjs = self._build_stale_varobjs(to_remove, to_reanchor)
        changelist = await self._update_unchanged_varobjs(gen, stale_varobjs)
        if self._rebuild_gen != gen:
            return

        to_reanchor = self._demote_out_of_scope_reanchors(to_reanchor, changelist, to_remove)
        if self._rebuild_gen != gen:
            return

        for binding_name, binding_addr, variable in to_reanchor:
            await self._reanchor_var(gen, binding_name, binding_addr, variable, tree)
            if self._rebuild_gen != gen:
                return

        if not await self._remove_gone_bindings(gen, to_remove):
            return

        restore: set[ExpansionPath] = set()
        if new_frame_key:
            restore = self._saved_expansions.get(new_frame_key, set())

        if not await self._add_new_bindings(gen, tree, to_add, restore, shadowed_keys):
            return

        self._sync_shadow_labels(shadowed_keys)


    async def _reanchor_var(self, gen: int, name: str, addr: str, outer_var: LocalVariable, tree: Tree) -> None:
        """Replace a floating varobj with an address-pinned one."""
        key = (name, addr)
        old_varobj = self._tracked.get(key, "")
        node = None
        if old_varobj:
            node = self._varobj_to_node.get(old_varobj)

        type_str = ""
        if old_varobj:
            type_str = self._varobj_type.get(old_varobj, "")

        if old_varobj:
            self._purge_varobj_subtree(old_varobj)
            if old_varobj in self._varobj_names:
                self._varobj_names.remove(old_varobj)
            self._pinned_varobjs.discard(old_varobj)
            if self._var_delete:
                try:
                    await self._var_delete(old_varobj)
                except Exception:
                    pass

        self._tracked.pop(key, None)
        if self._rebuild_gen != gen:
            return

        fallback_value = outer_var.value or "?"
        if not type_str:
            if node is not None:
                self._collapse_to_leaf_node(node, name, fallback_value, shadowed=True, compact_value=True)
            return

        addr_expr = f"*({type_str}*){addr}"
        try:
            info = await self._var_create(addr_expr)
        except Exception:
            if node is not None:
                self._collapse_to_leaf_node(node, name, fallback_value, shadowed=True, compact_value=True)
            return

        if self._rebuild_gen != gen:
            return

        new_varobj = self._remember_reanchored_varobj(key, info, type_str)
        value = info.get("value", outer_var.value or "")
        displayhint = info.get("displayhint", "")
        numchild = self._safe_int(info.get("numchild", "0"))
        has_children = (numchild > 0 or info.get("dynamic", "0") == "1") and not _suppress_children(info)

        if node is not None:
            node_data = node.data
            if isinstance(node_data, dict):
                node_data["varobj"] = new_varobj
                node_data["has_children"] = has_children
                node_data["displayhint"] = displayhint

            node.allow_expand = has_children
            if new_varobj:
                self._varobj_to_node[new_varobj] = node

            exp = name
            if isinstance(node_data, dict):
                exp = node_data.get("exp", name)

            label = self._build_value_label(exp, value, has_children, collapse_compound=True)
            node.set_label(f"{label}{_SHADOW_SUFFIX}")

            if isinstance(node_data, dict) and node_data.get("loaded") and has_children and new_varobj:
                node_data["loaded"] = False
                node.remove_children()
                node.add_leaf("⏳ loading...")
                asyncio.create_task(self._load_children(node, new_varobj))
            elif not has_children:
                self._collapse_to_leaf_node(node, exp, value, shadowed=True)

            return

        node = self._add_value_node(
            tree.root,
            name,
            value,
            has_children,
            varobj_name=new_varobj,
            displayhint=displayhint,
            shadow_suffix=_SHADOW_SUFFIX,
            collapse_compound=True,
        )
        if new_varobj:
            self._varobj_to_node[new_varobj] = node


    def _sync_shadow_labels(self, shadowed_keys: set[BindingKey]) -> None:
        for name_addr, varobj_name in self._tracked.items():
            if not varobj_name:
                continue

            node = self._varobj_to_node.get(varobj_name)
            if node is None:
                continue

            if hasattr(node.label, "plain"):
                label_plain = node.label.plain
            else:
                label_plain = str(node.label)

            is_currently_shadowed = label_plain.endswith(_SHADOW_SUFFIX)
            should_be_shadowed = name_addr in shadowed_keys
            if is_currently_shadowed == should_be_shadowed:
                continue

            if should_be_shadowed:
                node.set_label(f"{label_plain}{_SHADOW_SUFFIX}")
                continue

            node.set_label(label_plain.removesuffix(_SHADOW_SUFFIX))


    def _apply_changelist(self, changelist: list[dict], skip_varobjs: set[str] = frozenset()) -> None:
        """Apply ``-var-update`` results to the live tree."""
        _log.debug(f"changelist: {len(changelist)} changes")

        for change in changelist:
            varobj_name = change.get("name", "")
            if varobj_name in skip_varobjs:
                continue

            if change.get("in_scope", "true") != "true":
                continue

            if change.get("type_changed", "false") == "true":
                continue

            node = self._varobj_to_node.get(varobj_name)
            if node is None:
                continue

            data = node.data
            if not isinstance(data, dict):
                continue

            if data.get("load_more"):
                continue

            exp = data.get("exp", "")
            has_children = data.get("has_children", False)
            new_value = change.get("value", "")
            is_pinned = varobj_name in self._pinned_varobjs

            label = self._build_value_label(exp, new_value, has_children, collapse_compound=is_pinned)
            if is_pinned:
                label = f"{label}{_SHADOW_SUFFIX}"

            node.set_label(label)

            if change.get("new_num_children") is None or not data.get("loaded"):
                continue

            data["loaded"] = False
            node.remove_children()
            if node.is_expanded:
                asyncio.create_task(self._load_children(node, varobj_name))
            else:
                node.add_leaf("⏳ loading...")
