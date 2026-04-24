"""
State-computation helpers for the local-variable pane.
"""

from __future__ import annotations

from ..gdb_controller import Frame, LocalVariable
from .shared import BindingEntry, BindingKey, FrameKey, _is_child_of_any, _log


class LocalVariablePaneUpdateMixin:
    """State reconciliation and varobj lifecycle management."""

    def _shadowed_keys_from_locals(
        self,
        new_bindings: list[BindingEntry],
        locals_info: list[dict],
    ) -> set[BindingKey]:
        """Build ``shadowed_keys`` from the ``get_locals()`` result.

        ``get_locals()`` returns variables in block-walk order (innermost first)
        with ``is_shadowed=True`` on every entry whose name is already claimed
        by an inner scope.  We match by tracking the nth occurrence of each
        name: the ith entry for a name in ``new_bindings`` corresponds to the
        ith entry for that name in ``locals_info``.  This avoids address-format
        normalisation since both lists walk scopes in the same order.
        """
        shadow_by_name: dict[str, list[bool]] = {}
        for info in locals_info:
            name = info.get("name", "")
            if not name:
                continue
            shadow_by_name.setdefault(name, []).append(bool(info.get("is_shadowed", False)))

        shadowed_keys: set[BindingKey] = set()
        occurrence: dict[str, int] = {}

        for binding_name, binding_addr, _ in new_bindings:
            idx = occurrence.get(binding_name, 0)
            occurrence[binding_name] = idx + 1
            flags = shadow_by_name.get(binding_name, [])
            if idx < len(flags) and flags[idx]:
                shadowed_keys.add((binding_name, binding_addr))

        return shadowed_keys


    def _bindings_from_local_variables(
        self,
        variables: list[LocalVariable],
    ) -> tuple[list[BindingEntry], set[BindingKey], set[BindingKey]]:
        """Build binding structures from rich ``LocalVariable`` objects.

        Used when variables were produced by ``_publish_locals_async()``:
        each object carries its own ``addr`` (unique stack slot) and
        ``is_shadowed`` flag from GDB Python, so no separate ``&name``
        evaluation or decl-line filtering is needed.
        """
        new_bindings: list[BindingEntry] = []
        new_binding_keys: set[BindingKey] = set()
        shadowed_keys: set[BindingKey] = set()

        for variable in variables:
            addr = variable.addr or variable.type
            key = (variable.name, addr)
            new_bindings.append((variable.name, addr, variable))
            new_binding_keys.add(key)
            if variable.is_shadowed:
                shadowed_keys.add(key)

        return new_bindings, new_binding_keys, shadowed_keys


    def _build_safe_to_update_varobjs(self, safe_dynamic_updated: set[str], garbage_dynamic: set[str]) -> list[str]:
        safe_to_update: list[str] = []

        for varobj_name in self._varobj_to_node:
            if varobj_name in self._dynamic_varobjs:
                continue

            if _is_child_of_any(varobj_name, safe_dynamic_updated):
                continue

            if _is_child_of_any(varobj_name, garbage_dynamic):
                continue

            safe_to_update.append(varobj_name)

        return safe_to_update


    async def _do_var_update(self) -> list[dict]:
        """Update all tracked varobjs and return a merged changelist."""
        if not self._varobj_names:
            return []

        if not self._var_update:
            return []

        changelist: list[dict] = []

        if not self._dynamic_varobjs:
            try:
                result = await self._var_update("*")
            except Exception as exc:
                _log.warning(f"var_update(*) failed: {exc}")
                return changelist

            changelist.extend(result)
            return changelist

        safe_dynamic_updated: set[str] = set()
        garbage_dynamic: set[str] = set()

        if self._var_eval_expr:
            for varobj_name in self._dynamic_varobjs:
                try:
                    new_value = await self._var_eval_expr(varobj_name)
                    changelist.append({"name": varobj_name, "value": new_value})
                    _log.debug(f"dynamic root {varobj_name} value refreshed: {new_value!r}")
                except Exception as exc:
                    _log.warning(f"var_evaluate_expression {varobj_name} failed: {exc}")
                    new_value = ""

                length = self._parse_container_length(new_value)
                if length is not None and length < self._SAFE_CHILD_COUNT:
                    try:
                        result = await self._var_update(varobj_name, timeout=10.0)
                    except Exception as exc:
                        _log.warning(f"var_update {varobj_name} failed: {exc}")
                    else:
                        changelist.extend(result)
                        safe_dynamic_updated.add(varobj_name)
                        _log.debug(f"dynamic root {varobj_name} updated (length={length})")
                    continue

                garbage_dynamic.add(varobj_name)
                _log.debug(f"Skipping -var-update for {varobj_name} and its children: length={length} (likely garbage)")

        safe_to_update = self._build_safe_to_update_varobjs(safe_dynamic_updated, garbage_dynamic)

        for varobj_name in safe_to_update:
            try:
                result = await self._var_update(varobj_name, timeout=10.0)
            except Exception as exc:
                err = str(exc).lower()
                if "not found" in err or "usage" in err:
                    _log.debug(f"Purging stale varobj {varobj_name}: {exc}")
                    self._purge_varobj_subtree(varobj_name)
                else:
                    _log.warning(f"Skipped varobj {varobj_name} during update: {exc}")
                continue

            changelist.extend(result)

        return changelist


    async def _eval_variable_addresses(self, gen: int, variables: list[LocalVariable]) -> dict[str, str] | None:
        """Evaluate the stack address of each unique variable name."""
        addrs: dict[str, str] = {}

        if self._var_eval:
            for variable in variables:
                if variable.name in addrs:
                    continue

                if self._rebuild_gen != gen:
                    return None

                try:
                    addrs[variable.name] = await self._var_eval(f"&{variable.name}")
                except Exception:
                    addrs[variable.name] = variable.type

            return addrs

        for variable in variables:
            if variable.name in addrs:
                continue

            addrs[variable.name] = variable.type

        return addrs


    async def _filter_by_decl_lines(self, variables: list[LocalVariable], frame: Frame | None) -> list[LocalVariable]:
        """Remove variables whose constructor has not run yet."""
        current_line = 0
        if frame is not None:
            current_line = frame.line

        if current_line <= 0 or not self._get_decl_lines:
            return variables

        try:
            decl_lines = await self._get_decl_lines()
        except Exception as exc:
            _log.debug(f"get_decl_lines failed: {exc}")
            return variables

        if not decl_lines:
            return variables

        filtered: list[LocalVariable] = []
        innermost_seen: set[str] = set()

        for variable in variables:
            if variable.is_arg:
                filtered.append(variable)
                continue

            if variable.name in innermost_seen:
                filtered.append(variable)
                continue

            innermost_seen.add(variable.name)
            decl_line = decl_lines.get(variable.name, 0)
            if decl_line > 0 and current_line <= decl_line:
                _log.debug(f"Hiding uninitialized var {variable.name} (decl={decl_line} current={current_line})")
                continue

            filtered.append(variable)

        return filtered


    def _compute_bindings(self, variables: list[LocalVariable], addrs: dict[str, str]) -> tuple[list[BindingEntry], set[BindingKey], set[BindingKey]]:
        """Build binding entries and determine which ones are shadowed."""
        tracked_outer: dict[str, list[str]] = {}

        for tracked_name, tracked_addr in self._tracked:
            if addrs.get(tracked_name) == tracked_addr:
                continue

            outer_addrs = tracked_outer.setdefault(tracked_name, [])
            outer_addrs.append(tracked_addr)

        new_bindings: list[BindingEntry] = []
        seen_names: set[str] = set()

        for variable in variables:
            if variable.name not in seen_names:
                seen_names.add(variable.name)
                addr = addrs.get(variable.name, variable.type)
                new_bindings.append((variable.name, addr, variable))
                continue

            outer_addrs = tracked_outer.get(variable.name, [])
            if not outer_addrs:
                continue

            outer_addr = outer_addrs.pop(0)
            new_bindings.append((variable.name, outer_addr, variable))

        new_binding_keys: set[BindingKey] = set()
        shadowed_keys: set[BindingKey] = set()

        for binding_name, binding_addr, _ in new_bindings:
            key = (binding_name, binding_addr)
            new_binding_keys.add(key)
            if addrs.get(binding_name) != binding_addr:
                shadowed_keys.add(key)

        return new_bindings, new_binding_keys, shadowed_keys


    def _build_frame_key(self, frame: Frame | None, binding_keys: set[BindingKey]) -> FrameKey:
        if frame is None or not frame.func:
            return None

        path = frame.fullname or frame.file
        return (frame.func, path, frozenset(binding_keys))


    def _build_removed_bindings(self, current_keys: set[BindingKey], new_binding_keys: set[BindingKey]) -> list[BindingKey]:
        to_remove: list[BindingKey] = []

        for key in current_keys:
            if key in new_binding_keys:
                continue

            to_remove.append(key)

        return to_remove


    def _build_added_bindings(self, current_keys: set[BindingKey], new_bindings: list[BindingEntry]) -> list[BindingEntry]:
        to_add: list[BindingEntry] = []

        for binding_name, binding_addr, variable in new_bindings:
            key = (binding_name, binding_addr)
            if key in current_keys:
                continue

            to_add.append((binding_name, binding_addr, variable))

        return to_add


    def _build_reanchor_bindings(
        self,
        current_keys: set[BindingKey],
        new_bindings: list[BindingEntry],
        shadowed_keys: set[BindingKey],
    ) -> list[BindingEntry]:
        to_reanchor: list[BindingEntry] = []

        for binding_name, binding_addr, variable in new_bindings:
            key = (binding_name, binding_addr)
            if key not in current_keys:
                continue

            if key not in shadowed_keys:
                continue

            varobj_name = self._tracked.get(key, "")
            if not varobj_name:
                continue

            if varobj_name in self._pinned_varobjs:
                continue

            to_reanchor.append((binding_name, binding_addr, variable))

        return to_reanchor


    def _build_stale_varobjs(self, to_remove: list[BindingKey], to_reanchor: list[BindingEntry]) -> set[str]:
        stale_varobjs: set[str] = set()

        for binding_name, binding_addr in to_remove:
            varobj_name = self._tracked.get((binding_name, binding_addr), "")
            if varobj_name:
                stale_varobjs.add(varobj_name)

        for binding_name, binding_addr, _ in to_reanchor:
            varobj_name = self._tracked.get((binding_name, binding_addr), "")
            if varobj_name:
                stale_varobjs.add(varobj_name)

        return stale_varobjs


    async def _update_unchanged_varobjs(self, gen: int, stale_varobjs: set[str]) -> list[dict]:
        if not self._varobj_names:
            return []

        try:
            changelist = await self._do_var_update()
        except Exception:
            return []

        if self._rebuild_gen == gen:
            self._apply_changelist(changelist, skip_varobjs=stale_varobjs)

        return changelist


    def _demote_out_of_scope_reanchors(
        self,
        to_reanchor: list[BindingEntry],
        changelist: list[dict],
        to_remove: list[BindingKey],
    ) -> list[BindingEntry]:
        if not to_reanchor or not changelist:
            return to_reanchor

        out_of_scope_varobjs: set[str] = set()

        for change in changelist:
            if not isinstance(change, dict):
                continue

            if change.get("in_scope") != "false":
                continue

            varobj_name = change.get("name", "")
            if varobj_name:
                out_of_scope_varobjs.add(varobj_name)

        if not out_of_scope_varobjs:
            return to_reanchor

        remaining_reanchors: list[BindingEntry] = []

        for binding_name, binding_addr, variable in to_reanchor:
            key = (binding_name, binding_addr)
            varobj_name = self._tracked.get(key, "")
            if varobj_name and varobj_name in out_of_scope_varobjs:
                to_remove.append(key)
                continue

            remaining_reanchors.append((binding_name, binding_addr, variable))

        return remaining_reanchors
