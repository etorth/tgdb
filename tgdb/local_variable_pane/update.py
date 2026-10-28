"""
State-computation helpers for the local-variable pane.
"""

from ..gdb_controller import Frame, LocalVariable
from ..gdb_controller.errors import GDBRequestTimeout
from ..gdb_controller.types import normalize_addr
from .shared import BindingEntry, BindingKey, FrameKey, _is_child_of_any, _log, _type_needs_name_fallback


class LocalVariablePaneUpdateMixin:
    """State reconciliation and varobj lifecycle management."""

    @staticmethod
    def _should_stop_varobj_refresh_after_error(exc: Exception) -> bool:
        """Return True when continuing this refresh would only queue stale MI."""
        if isinstance(exc, GDBRequestTimeout):
            return True

        message = str(exc).lower()
        return (
            "gdb process exited" in message
            or "gdb controller terminated" in message
            or "mi channel" in message
            or "mi command timed out" in message
        )


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

            # Exclude any varobj that is a descendant of ANY dynamic
            # varobj — including non-root ones such as
            # ``var36.public.index`` (a std::vector that is a member of
            # a non-dynamic struct).  Their children's value/access
            # info is owned by the parent printer and refreshes through
            # the parent's ``-var-update`` cascade.  Issuing
            # ``-var-update`` on the child directly can hit GDB
            # asserts (e.g. ``c-varobj.c:860 cplus_describe_child:
            # Assertion 'access' failed`` on
            # ``var36.public.index.[N]``) that crash the entire debug
            # session.  Checking ``self._dynamic_varobjs`` here
            # subsumes the prior ``safe_dynamic_updated`` /
            # ``garbage_dynamic`` checks (those are subsets) and also
            # catches the non-root case those did not cover.
            if _is_child_of_any(varobj_name, self._dynamic_varobjs):
                continue

            safe_to_update.append(varobj_name)

        return safe_to_update


    def _build_dynamic_root_value_overrides(
        self,
        variables: list[LocalVariable],
    ) -> dict[str, str]:
        """Map non-string dynamic roots to fresh values from the locals snapshot.

        GDB's ``-var-evaluate-expression`` returns a stale (or garbage)
        ``to_string()`` for dynamic varobjs whose pretty-printer's contained
        alternative changed under fixed storage — e.g. ``std::variant`` flipping
        between ``string`` and ``vector<int>``, ``std::optional`` toggling,
        ``std::any`` rebinding.  The socket-backed locals helper does a fresh
        GDB-Python value formatting pass at each stop, so use it to override
        those dynamic root labels.

        Do not override string displayhints: normal string varobjs already
        refresh their root value correctly through ``-var-update``, and the
        socket snapshot is only needed for dynamic printers with stale
        contained-state summaries.
        """
        overrides: dict[str, str] = {}
        if not self._dynamic_varobjs or not variables:
            return overrides

        for variable in variables:
            addr = variable.addr or variable.type
            varobj_name = self._varobj_of((variable.name, addr))
            if not varobj_name or varobj_name not in self._dynamic_varobjs:
                continue

            node = self._varobj_to_node.get(varobj_name)
            data = node.data if node is not None else None
            if isinstance(data, dict) and data.get("displayhint") == "string":
                continue

            if variable.value:
                overrides[varobj_name] = variable.value

        return overrides


    async def _do_var_update(
        self,
        dynamic_root_overrides: dict[str, str] | None = None,
    ) -> list[dict]:
        """Update all tracked varobjs and return a merged changelist."""
        root_varobjs = self._root_varobjs()
        if not root_varobjs:
            return []

        if not self._var_update:
            return []

        changelist: list[dict] = []

        if not self._dynamic_varobjs:
            # Update only this pane's own roots (children follow their
            # root).  ``-var-update *`` would also consume the change
            # reports of other panes' varobjs (e.g. evaluate watches).
            for varobj_name in root_varobjs:
                try:
                    result = await self._var_update(varobj_name)
                except Exception as exc:
                    err = str(exc).lower()
                    if "not found" in err or "usage" in err:
                        _log.debug(f"Purging stale varobj {varobj_name}: {exc}")
                        self._purge_stale_varobj(varobj_name)
                    else:
                        _log.warning(f"var_update {varobj_name} failed: {exc}")
                        if self._should_stop_varobj_refresh_after_error(exc):
                            return changelist
                    continue

                changelist.extend(result)
            return changelist

        safe_dynamic_updated: set[str] = set()
        garbage_dynamic: set[str] = set()
        overrides = dynamic_root_overrides or {}

        # Iterate only dynamic ROOT varobjs.  ``_dynamic_varobjs`` also
        # contains dynamic descendants — children that GDB marked
        # ``dynamic="1"`` (e.g. ``var1.[contained value]``, the
        # std::string contained inside a std::variant) — but those get
        # their changes piggybacked through the root's ``-var-update``
        # changelist.  Issuing ``-var-update`` directly on a stale
        # dynamic CHILD whose parent printer's contained type just
        # changed (variant alternative switch, optional toggle, any
        # rebind) crashes GDB at gdb/varobj.c:1298 with
        # ``install_new_value: Assertion '!var->value->lazy()'`` because
        # GDB's varobj cache for that child is in an inconsistent state
        # post-transition.  Limit the iteration to the bindings' roots.
        dynamic_roots = [n for n in root_varobjs if n in self._dynamic_varobjs]

        if self._var_eval_expr:
            for varobj_name in dynamic_roots:
                override = overrides.get(varobj_name)
                if override is not None:
                    new_value = override
                    changelist.append({"name": varobj_name, "value": new_value})
                    _log.debug(f"dynamic root {varobj_name} value from locals: {new_value!r}")
                else:
                    try:
                        new_value = await self._var_eval_expr(varobj_name)
                        changelist.append({"name": varobj_name, "value": new_value})
                        _log.debug(f"dynamic root {varobj_name} value refreshed: {new_value!r}")
                    except Exception as exc:
                        _log.warning(f"var_evaluate_expression {varobj_name} failed: {exc}")
                        if self._should_stop_varobj_refresh_after_error(exc):
                            return changelist
                        new_value = ""

                length = self._parse_container_length(new_value)
                if length is None or length < self._SAFE_CHILD_COUNT:
                    try:
                        result = await self._var_update(varobj_name, timeout=10.0)
                    except Exception as exc:
                        _log.warning(f"var_update {varobj_name} failed: {exc}")
                        if self._should_stop_varobj_refresh_after_error(exc):
                            return changelist
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
                    self._purge_stale_varobj(varobj_name)
                else:
                    _log.warning(f"Skipped varobj {varobj_name} during update: {exc}")
                    if self._should_stop_varobj_refresh_after_error(exc):
                        return changelist
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
                    # MI ``-data-evaluate-expression "&name"`` returns
                    # ``"(int *) 0x...."``; normalise to a bare ``0x...``
                    # so BindingKeys match the fast-path format.
                    addrs[variable.name] = normalize_addr(await self._var_eval(f"&{variable.name}"))
                except Exception:
                    addrs[variable.name] = variable.type

            return addrs

        for variable in variables:
            if variable.name in addrs:
                continue

            addrs[variable.name] = variable.type

        return addrs


    def _compute_bindings(self, variables: list[LocalVariable], addrs: dict[str, str]) -> tuple[list[BindingEntry], set[BindingKey], set[BindingKey]]:
        """Build binding entries and determine which ones are shadowed."""
        tracked_outer: dict[str, list[str]] = {}

        for binding in self._tracked_bindings():
            tracked_name, tracked_addr = binding.key
            if addrs.get(tracked_name) == tracked_addr:
                continue

            outer_addrs = tracked_outer.setdefault(tracked_name, [])
            outer_addrs.append(tracked_addr)

        new_bindings: list[BindingEntry] = []
        seen_names: set[str] = set()
        outer_occurrence: dict[str, int] = {}

        for variable in variables:
            if variable.name not in seen_names:
                seen_names.add(variable.name)
                addr = addrs.get(variable.name, variable.type)
                new_bindings.append((variable.name, addr, variable))
                continue

            outer_addrs = tracked_outer.get(variable.name, [])
            if outer_addrs:
                outer_addr = outer_addrs.pop(0)
            else:
                # Cold start (no previous tracking) for an outer
                # same-named variable.  The slow path gets each
                # ``addrs[name]`` from a single ``&name`` evaluation,
                # which GDB resolves to the innermost scope only — so
                # there's no way to recover a real address for the
                # outer.  Synthesize a stable-ish key from the type
                # string and the occurrence index so the outer at
                # least gets a distinct BindingKey instead of
                # colliding with the inner and being silently dropped.
                idx = outer_occurrence.get(variable.name, 0)
                outer_occurrence[variable.name] = idx + 1
                outer_addr = f"{variable.type}#outer{idx}"
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
        # ``frame.level`` is required to disambiguate recursive calls of
        # the same function: without it, every level of a recursion whose
        # only locals live in registers / get the same ``addr`` sentinel
        # collapses to one FrameKey, so expansion state saved at the deep
        # frame is restored at every outer frame on unwind.  Including the
        # level keeps each recursion depth's expansion slot distinct.
        return (frame.func, path, frame.level, frozenset(binding_keys))


    def _build_removed_bindings(self, current_keys: set[BindingKey], new_binding_keys: set[BindingKey]) -> list[BindingKey]:
        to_remove: list[BindingKey] = []

        for key in current_keys:
            if key in new_binding_keys:
                continue

            to_remove.append(key)

        return to_remove


    def _build_added_bindings(
        self,
        current_keys: set[BindingKey],
        new_bindings: list[BindingEntry],
        shadowed_keys: set[BindingKey],
    ) -> list[BindingEntry]:
        to_add: list[BindingEntry] = []

        for binding_name, binding_addr, variable in new_bindings:
            key = (binding_name, binding_addr)
            if key in current_keys:
                continue
            # Permanent placeholders (unparseable types whose
            # depth > min_depth so they cannot be promoted to a real
            # varobj) have no varobj, so they are NOT in
            # ``current_keys`` (the tracked keys).  Without this skip, every refresh
            # would rediscover them as "new", call
            # ``_remove_placeholder_node`` (destroying the node), then
            # immediately re-add a fresh placeholder.  The user sees a
            # flicker on every stop and loses tree-cursor focus on
            # those nodes.
            #
            # Only skip placeholders that cannot be fixed by a plain
            # retry: unparseable types (``_promote_placeholders`` owns
            # their retry) and shadowed bindings (name-based creation
            # would bind to the inner same-named variable).  Other
            # placeholders — ``<not yet initialized>`` memory, failed
            # ``-var-create`` — must be retried, otherwise they stay
            # frozen on their first value for as long as they are in
            # scope.
            existing = self._bindings.get(key)
            if existing is not None and existing.is_placeholder:
                if _type_needs_name_fallback(variable.type or "") or key in shadowed_keys:
                    continue

            to_add.append((binding_name, binding_addr, variable))

        return to_add


    def _build_frame_bound_rebindings(
        self,
        current_keys: set[BindingKey],
        new_bindings: list[BindingEntry],
        new_frame_key: FrameKey,
    ) -> list[BindingEntry]:
        """Return kept bindings whose varobj is still bound to the previous frame.

        Varobjs created by name (``-var-create - * name``) are bound to the
        frame selected at creation time.  Bindings without a real stack
        address (``register@N``, ``unknown``) produce the same BindingKey in
        every frame, so after ``up`` / ``down`` / ``step`` / ``finish`` into a
        different frame the kept varobj would keep showing the old frame's
        variable.  Address-pinned varobjs are frame-independent and are not
        returned.
        """
        rebind: list[BindingEntry] = []
        old_frame_key = self._frame_key
        if old_frame_key is None or new_frame_key is None:
            return rebind

        if old_frame_key[:3] == new_frame_key[:3]:
            return rebind

        for binding_name, binding_addr, variable in new_bindings:
            key = (binding_name, binding_addr)
            if key not in current_keys:
                continue

            if binding_addr.startswith("0x"):
                continue

            binding = self._bindings.get(key)
            if binding is None or binding.is_placeholder or binding.pinned:
                continue

            rebind.append((binding_name, binding_addr, variable))

        return rebind


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

            binding = self._bindings.get(key)
            if binding is None or binding.is_placeholder or binding.pinned:
                continue

            # Fixed varobjs for unparseable types (anonymous namespace) are
            # permanently bound to their original stack slot.  They remain
            # valid even when shadowed — skip reanchoring.
            if _type_needs_name_fallback(binding.type):
                continue

            # References are created by name for the same reason: their
            # ``addr`` is the referent's address and their ``T &`` type is
            # not usable in ``*(type*)addr``, so a reanchor would either
            # fail (and collapse the node) or reinterpret the referent's
            # bytes as a reference.  The fixed varobj stays bound to the
            # original symbol when shadowed.
            if variable.is_reference:
                continue

            to_reanchor.append((binding_name, binding_addr, variable))

        return to_reanchor


    def _build_stale_varobjs(self, to_remove: list[BindingKey], to_reanchor: list[BindingEntry]) -> set[str]:
        stale_varobjs: set[str] = set()

        for key in to_remove:
            varobj_name = self._varobj_of(key)
            if varobj_name:
                stale_varobjs.add(varobj_name)

        for binding_name, binding_addr, _ in to_reanchor:
            varobj_name = self._varobj_of((binding_name, binding_addr))
            if varobj_name:
                stale_varobjs.add(varobj_name)

        return stale_varobjs


    async def _update_unchanged_varobjs(
        self,
        gen: int,
        stale_varobjs: set[str],
        dynamic_root_overrides: dict[str, str] | None = None,
    ) -> list[dict]:
        if not self._root_varobjs():
            return []

        try:
            changelist = await self._do_var_update(dynamic_root_overrides)
        except Exception:
            return []

        if self._rebuild_gen == gen:
            await self._apply_changelist(changelist, skip_varobjs=stale_varobjs)

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
            varobj_name = self._varobj_of(key)
            if varobj_name and varobj_name in out_of_scope_varobjs:
                to_remove.append(key)
                continue

            remaining_reanchors.append((binding_name, binding_addr, variable))

        return remaining_reanchors
