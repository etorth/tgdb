"""
Reconciliation helpers for the local-variable pane.
"""

from textual.widgets import Tree

from ..async_util import supervise
from ..gdb_controller import Frame, LocalVariable
from .shared import BindingEntry, BindingKey, ExpansionPath, _log, _suppress_children, _type_needs_name_fallback


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
                    _log.debug(
                        f"-var-delete {varobj_name} failed", exc_info=True,
                    )

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
        min_depth_by_name: dict[str, int],
    ) -> bool:
        binding_name, binding_addr, variable = binding
        key = (binding_name, binding_addr)
        self._remove_placeholder_node(key)

        marker_active = self._binding_marker_active(key, shadowed_keys)
        stack_value = variable.value or ""

        if "<error reading variable:" in stack_value:
            label = self._build_value_label(variable.name, "<initializing>", False, marker_active=marker_active)
            self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
            _log.debug(f"Skipping var_create for {variable.name}: uninitialized memory in bvar.value ({stack_value[:80]!r})")
            return True

        # Use address-based expression when we have a precise address from
        # get_locals_b64(), so GDB binds to the right object even when another
        # same-named variable is in scope at the current PC.
        # References are excluded: their type includes "&" which is not valid
        # in a C cast expression.
        type_str = variable.type or ""
        use_addr = (
            binding_addr
            and binding_addr not in ("register", "unknown", "")
            and not variable.is_reference
            and type_str
        )

        # Types from anonymous namespaces (e.g. "(anonymous namespace)::Foo")
        # cannot be used in cast expressions — GDB's parser chokes on the
        # parentheses.  Fall back to creating the varobj by plain name.
        # This only works for the innermost variable when shadowed: GDB
        # resolves the name to the innermost scope.  Outer same-named
        # variables with unparseable types become placeholders.
        if use_addr and _type_needs_name_fallback(type_str):
            return await self._add_binding_by_name_fallback(
                gen, tree, binding, restore, shadowed_keys, marker_active, min_depth_by_name,
            )

        var_expr = f"*({type_str}*){binding_addr}" if use_addr else variable.name

        try:
            info = await self._var_create(var_expr)
        except Exception:
            value = variable.value.replace("\n", " ")
            if not value:
                value = "<complex>"

            label = self._build_value_label(variable.name, value, False, marker_active=marker_active)
            self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
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
                    _log.debug(
                        f"-var-delete {varobj_name} failed", exc_info=True,
                    )

            label = self._build_value_label(variable.name, "<not yet initialized>", False, marker_active=marker_active)
            self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
            _log.debug(f"var_create {variable.name}: uninitialized memory in response ({value[:80]!r}), placeholder added")
            return True

        varobj_name = self._remember_root_varobj(key, info, is_pinned=use_addr)
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
            marker_active=marker_active,
        )
        if varobj_name:
            self._varobj_to_node[varobj_name] = node

        return await self._restore_binding_expansion(gen, tree, restore, binding_name)


    async def _add_binding_by_name_fallback(
        self,
        gen: int,
        tree: Tree,
        binding: BindingEntry,
        restore: set[ExpansionPath],
        shadowed_keys: set[BindingKey],
        marker_active: bool,
        min_depth_by_name: dict[str, int],
    ) -> bool:
        """Create a varobj by plain name for a variable whose type is unparseable.

        When the type contains ``(anonymous namespace)`` the address-based cast
        expression ``*(type*)addr`` fails in GDB's parser.  We fall back to
        creating the varobj by the variable's plain name.

        GDB resolves the name to the innermost scope, so only the variable with
        the smallest depth for its name can claim the name.  Others become
        placeholders.

        Placeholders are promoted to real varobjs when scope changes make
        name-based creation possible again (handled by ``_promote_placeholders``
        during reconciliation).
        """
        binding_name, binding_addr, variable = binding
        key = (binding_name, binding_addr)

        # Only the variable with the smallest depth for this name can be
        # created by plain name — GDB resolves to the innermost scope.
        is_smallest_depth = (variable.depth == min_depth_by_name.get(binding_name, 0))

        if not is_smallest_depth:
            # Not the innermost — must be a placeholder.
            value_display = variable.value or variable.addr or "?"
            label = self._build_value_label(variable.name, value_display, False, marker_active=marker_active)
            self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
            _log.debug(
                f"Placeholder for shadowed {variable.name} at {binding_addr}: "
                f"depth {variable.depth} > min {min_depth_by_name.get(binding_name)}"
            )
            return True

        # Innermost (or only) variable — create by plain name.
        # Fixed varobjs are permanently bound at creation, so even if another
        # varobj for the same name at a different address already exists, both
        # can coexist safely (they track different stack slots).
        try:
            info = await self._var_create(variable.name)
        except Exception:
            value = variable.value.replace("\n", " ")
            if not value:
                value = "<complex>"
            label = self._build_value_label(variable.name, value, False, marker_active=marker_active)
            self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
            _log.debug(f"var_create by name failed for {variable.name}: fallback to placeholder")
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
                    _log.debug(f"-var-delete {varobj_name} failed", exc_info=True)

            label = self._build_value_label(variable.name, "<not yet initialized>", False, marker_active=marker_active)
            self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
            return True

        # Name-based varobjs are NOT pinned (is_pinned=False) — they were
        # created by name, not by address cast.  They still have fixed binding
        # to the resolved stack slot.
        varobj_name = self._remember_root_varobj(key, info, is_pinned=False)
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
            marker_active=marker_active,
        )
        if varobj_name:
            self._varobj_to_node[varobj_name] = node

        _log.debug(
            f"var_create by name for {variable.name} (type {variable.type!r} unparseable): "
            f"varobj={varobj_name}"
        )
        return await self._restore_binding_expansion(gen, tree, restore, binding_name)


    async def _add_new_bindings(
        self,
        gen: int,
        tree: Tree,
        to_add: list[BindingEntry],
        restore: set[ExpansionPath],
        shadowed_keys: set[BindingKey],
        all_bindings: list[BindingEntry],
    ) -> bool:
        if not self._var_create:
            return True

        # Precompute: for each variable name, find the minimum depth across
        # ALL variables with that name (not just unparseable ones).  GDB's
        # name resolution always resolves to the innermost scope, so if a
        # parseable variable shadows an unparseable one, name-based creation
        # would bind to the wrong variable.
        min_depth_by_name: dict[str, int] = {}
        for _, _, var in all_bindings:
            prev = min_depth_by_name.get(var.name)
            if prev is None or var.depth < prev:
                min_depth_by_name[var.name] = var.depth

        for binding in to_add:
            if self._rebuild_gen != gen:
                return False

            if not await self._add_new_binding(gen, tree, binding, restore, shadowed_keys, min_depth_by_name):
                return False

        return True


    async def _promote_placeholders(
        self,
        gen: int,
        tree: Tree,
        new_bindings: list[BindingEntry],
        restore: set[ExpansionPath],
        shadowed_keys: set[BindingKey],
    ) -> set[BindingKey]:
        """Promote placeholders to real varobjs when name-based creation is possible.

        A placeholder is promotable when its (name, addr) matches a variable
        in the current variable list AND that variable has the smallest depth
        among all same-named variables.  GDB resolves plain names to the
        innermost scope, so only the smallest-depth variable can be correctly
        created by name.

        Returns the set of (name, addr) keys that were successfully promoted —
        the caller must filter these out of any subsequent ``to_add`` list to
        avoid creating duplicate nodes for the same binding.
        """
        promoted: set[BindingKey] = set()
        if not self._var_create:
            return promoted

        # Build lookup: (name, addr) → variable, and name → min depth.
        var_by_key: dict[BindingKey, LocalVariable] = {}
        min_depth_by_name: dict[str, int] = {}
        for _, _, variable in new_bindings:
            key = (variable.name, variable.addr or variable.type)
            var_by_key[key] = variable
            prev = min_depth_by_name.get(variable.name)
            if prev is None or variable.depth < prev:
                min_depth_by_name[variable.name] = variable.depth

        for key in list(self._uninitialized_nodes.keys()):
            if self._rebuild_gen != gen:
                return promoted

            placeholder_name, placeholder_addr = key
            variable = var_by_key.get(key)
            if variable is None:
                continue

            if not _type_needs_name_fallback(variable.type or ""):
                continue

            # Only the variable with the smallest depth for this name can be
            # created by plain name — GDB resolves to the innermost scope.
            if variable.depth != min_depth_by_name.get(placeholder_name):
                continue

            # Promote: remove placeholder, create real varobj by name.
            # Multiple fixed varobjs for the same name at different addresses
            # can coexist safely — each is permanently bound to its stack slot.
            marker_active = self._binding_marker_active(key, shadowed_keys)
            self._remove_placeholder_node(key)

            try:
                info = await self._var_create(variable.name)
            except Exception:
                value = variable.value.replace("\n", " ") or "<complex>"
                label = self._build_value_label(variable.name, value, False, marker_active=marker_active)
                self._add_placeholder_node(tree, key, variable.name, label, marker_active=marker_active)
                _log.debug(f"Placeholder promotion failed for {variable.name}: var_create by name failed")
                continue

            if self._rebuild_gen != gen:
                return promoted

            value = info.get("value", "")
            varobj_name = self._remember_root_varobj(key, info, is_pinned=False)
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
                marker_active=marker_active,
            )
            if varobj_name:
                self._varobj_to_node[varobj_name] = node

            promoted.add(key)
            _log.debug(f"Promoted placeholder {variable.name} at {placeholder_addr} to varobj={varobj_name}")

            await self._restore_binding_expansion(gen, tree, restore, variable.name)

        return promoted


    async def _update_variables(self, gen: int, frame: Frame | None, variables: list[LocalVariable]) -> None:
        """Incrementally reconcile the locals tree with the current frame."""
        if self._rebuild_gen != gen:
            return

        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        # Fast path: variables published by _publish_locals_async() carry addr
        # and is_shadowed directly from GDB Python — no extra MI round-trips.
        if variables and variables[0].addr:
            new_bindings, new_binding_keys, shadowed_keys = self._bindings_from_local_variables(variables)
        else:
            # Fallback: evaluate addresses via &name.
            addrs = await self._eval_variable_addresses(gen, variables)
            if addrs is None:
                return

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

        # Promote placeholders whose unparseable-type variable is no longer
        # shadowed.  This must happen before _add_new_bindings so we don't
        # try to add a binding that was just promoted.
        promoted = await self._promote_placeholders(gen, tree, new_bindings, restore, shadowed_keys)
        if self._rebuild_gen != gen:
            return

        # Filter out keys that were just promoted from placeholders — those
        # already have real varobj nodes and must not be re-added.
        # No sort: preserve original order from get_locals_b64() (declaration
        # order, newest last).
        to_add_filtered = [b for b in to_add if (b[0], b[1]) not in promoted]

        if not await self._add_new_bindings(gen, tree, to_add_filtered, restore, shadowed_keys, new_bindings):
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
                self._collapse_to_leaf_node(node, name, fallback_value, compact_value=True, marker_active=False)
            return

        # Unparseable types (e.g. anonymous namespace) cannot be reanchored
        # via address cast — remove the old node and add a placeholder.
        if _type_needs_name_fallback(type_str):
            if node is not None:
                node.remove()
            label = self._build_value_label(name, fallback_value, False, marker_active=False)
            self._add_placeholder_node(tree, key, name, label, marker_active=False)
            _log.debug(f"Reanchor {name} at {addr}: type {type_str!r} unparseable, demoted to placeholder")
            return

        addr_expr = f"*({type_str}*){addr}"
        try:
            info = await self._var_create(addr_expr)
        except Exception:
            if node is not None:
                self._collapse_to_leaf_node(node, name, fallback_value, compact_value=True, marker_active=False)
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
                node_data["marker_active"] = False

            node.allow_expand = has_children
            if new_varobj:
                self._varobj_to_node[new_varobj] = node

            exp = name
            if isinstance(node_data, dict):
                exp = node_data.get("exp", name)

            label = self._build_value_label(exp, value, has_children, collapse_compound=True, marker_active=False)
            node.set_label(label)

            if isinstance(node_data, dict) and node_data.get("loaded") and has_children and new_varobj:
                node_data["loaded"] = False
                node.remove_children()
                node.add_leaf("⏳ loading...")
                supervise(self._load_children(node, new_varobj), name="locals-load-children")
            elif not has_children:
                self._collapse_to_leaf_node(node, exp, value, marker_active=False)

            return

        node = self._add_value_node(
            tree.root,
            name,
            value,
            has_children,
            varobj_name=new_varobj,
            displayhint=displayhint,
            collapse_compound=True,
            marker_active=False,
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

            data = node.data
            if not isinstance(data, dict):
                continue

            current_marker_active = data.get("marker_active", True)
            new_marker_active = name_addr not in shadowed_keys
            if new_marker_active == current_marker_active:
                continue

            self._set_node_marker_active(node, new_marker_active)


    def _apply_changelist(self, changelist: list[dict], skip_varobjs: frozenset[str] = frozenset()) -> None:
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
            marker_active = data.get("marker_active", True)

            label = self._build_value_label(
                exp,
                new_value,
                has_children,
                collapse_compound=is_pinned,
                marker_active=marker_active,
            )

            node.set_label(label)

            if change.get("new_num_children") is None or not data.get("loaded"):
                continue

            data["loaded"] = False
            node.remove_children()
            if node.is_expanded:
                supervise(self._load_children(node, varobj_name), name="locals-load-children")
            else:
                node.add_leaf("⏳ loading...")
