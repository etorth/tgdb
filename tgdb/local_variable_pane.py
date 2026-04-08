"""
Local variables pane widget — tree view with lazy varobj expansion.

Uses GDB's ``-var-create`` / ``-var-list-children`` / ``-var-update`` /
``-data-evaluate-expression`` MI commands to maintain a structured,
expandable tree of local variables and their members.

Variable identity is based on the stack address of each variable.  This
lets us do incremental per-variable updates instead of rebuilding the
whole tree:

* Variable unchanged (same name, same address) → update value in-place,
  expansion state preserved.
* Variable changed (address moved — inner-scope shadow) → delete old
  varobj and tree node, create new one (starts collapsed).
* Variable disappeared → deleted from tree.
* New variable appeared → created, added to tree (starts collapsed).
* Outer (shadowed) bindings of the same name → shown as a non-expandable
  leaf so the user can see the value without disrupting the inner binding.

Expansion state is saved/restored keyed by
``(func, file, frozenset{(name, addr)})``.  Different activations of
the same function (recursive calls) differ in stack addresses, so each
gets its own saved state.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .gdb_controller import LocalVariable, Frame
from .highlight_groups import HighlightGroups
from .config_types import Config
from .pane_base import PaneBase

_log = logging.getLogger("tgdb.locals")


# ---------------------------------------------------------------------------
# Varobj display helpers
# ---------------------------------------------------------------------------


def _suppress_children(varobj_info: dict) -> bool:
    """Return True when the varobj should be shown as a non-expandable leaf.

    GDB's pretty-printer framework sets ``displayhint = "string"`` for every
    string-like type whose printer returns ``display_hint() = 'string'``,
    including ``std::string`` / ``std::wstring`` / ``std::u8string`` /
    ``std::u16string`` / ``std::u32string`` and any future string type whose
    pretty-printer follows the same convention.

    Note: raw C-string pointer types (``char *``, ``const char *``, etc.) are
    handled by GDB's *built-in* printer rather than a Python pretty-printer, so
    they do **not** receive ``displayhint = "string"``.  They will appear as
    expandable nodes; their value already shows the full string inline
    (``0x... "hello"``), so expansion is simply unnecessary, not harmful.
    """
    return varobj_info.get("displayhint", "") == "string"


class LocalVariablePane(PaneBase):
    """Render the current frame's local variables as an expandable tree."""

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

    def __init__(self, hl: HighlightGroups, cfg: Config, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._cfg = cfg
        self._variables: list[LocalVariable] = []

        # Async callbacks wired up by the app
        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        self._var_update: Optional[Callable[..., Coroutine]] = None
        self._var_eval: Optional[Callable[..., Coroutine]] = None
        self._var_eval_expr: Optional[Callable[..., Coroutine]] = None
        self._get_decl_lines: Optional[Callable[..., Coroutine]] = None

        # Per-variable state keyed by (name, address) so every distinct
        # binding of the same name is tracked independently — no same-name
        # collisions even when inner scopes shadow outer variables.
        # (name, addr) → varobj_name
        self._tracked: dict[tuple[str, str], str] = {}

        # Varobjs created with *(Type*)addr rather than the plain variable
        # name.  Needed for outer (shadowed) bindings: GDB's floating varobj
        # (created with the plain name) gets hijacked to follow the inner
        # binding when a new inner-scope variable with the same name appears.
        # Pinning it to a fixed address prevents that.
        self._pinned_varobjs: set[str] = set()

        # Type string saved from the var_create response.
        # Used by _reanchor_var to build *(Type*)addr for address-pinned
        # shadow varobjs.  Populated from the "type" field that GDB returns
        # in the var_create result (which -stack-list-variables never returns).
        # varobj_name → type_str
        self._varobj_type: dict[str, str] = {}

        # varobj name → TreeNode (all depths)
        self._varobj_to_node: dict[str, TreeNode] = {}

        # All live GDB varobj names (for cleanup)
        self._varobj_names: list[str] = []

        # Varobjs backed by a pretty-printer (dynamic=1).
        # These are NEVER passed to -var-update because that requires GDB to
        # re-run the container's children() iterator, which hangs the MI
        # channel for uninitialized containers with garbage sizes.
        # Instead, their display value is refreshed via -var-evaluate-expression
        # (calls to_string() only) and their child varobjs are updated individually.
        self._dynamic_varobjs: set[str] = set()

        # Placeholder tree nodes for variables that are in scope but whose
        # memory is not yet accessible (e.g. a std::map initialised over
        # multiple source lines: GDB sees the variable as "in scope" from the
        # opening brace, but its storage is garbage until the constructor runs).
        # These keys are intentionally NOT stored in _tracked so that the next
        # stop's rebuild will retry var_create automatically.
        # (name, addr) -> leaf TreeNode showing the placeholder
        self._uninitialized_nodes: dict[tuple[str, str], "TreeNode"] = {}

        # Frame key for expansion save/restore.
        # Type: (func, file, frozenset{(name, addr)}) | None
        self._frame_key: tuple | None = None

        # Saved expansion states keyed by frame key.
        self._saved_expansions: dict[tuple, set[tuple[str, ...]]] = {}

        # Generation counter — stale async tasks check this and abort.
        self._rebuild_gen: int = 0

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
        var_eval: Callable[..., Coroutine],
        var_eval_expr: Callable[..., Coroutine],
        get_decl_lines: Callable[..., Coroutine],
    ) -> None:
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete
        self._var_update = var_update
        self._var_eval = var_eval
        self._var_eval_expr = var_eval_expr
        self._get_decl_lines = get_decl_lines

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def set_variables(
        self,
        variables: list[LocalVariable],
        frame: Frame | None = None,
    ) -> None:
        """Called by the app when GDB stops.

        An empty list means the inferior is running (* running event).
        We cancel any pending task but leave the tree intact so the next
        stop can do an incremental update.
        """
        if not variables:
            self._rebuild_gen += 1
            return
        self._variables = list(variables)
        self._rebuild_gen += 1
        asyncio.create_task(self._update_variables(self._rebuild_gen, frame, variables))

    # ------------------------------------------------------------------
    # Core update logic
    # ------------------------------------------------------------------

    # GDB pretty-printers use two formats for container sizes:
    #   "std::vector of length 2, capacity 2"  -> group(1) matches "length 2"
    #   "std::map with 3 elements"             -> group(2) matches "with 3 elements"
    # Sets, unordered_map, unordered_set, etc. all use the "with N elements" form.
    _RE_CONTAINER_LENGTH = re.compile(
        r"(?:length|size)\s+(\d+)|with\s+(\d+)\s+elements",
        re.IGNORECASE,
    )
    # Threshold above which a reported container length is treated as garbage
    # (i.e. from an uninitialized variable).  Sending -var-update to GDB for
    # a container with a "length" of 10^18 causes GDB to iterate that many
    # elements and hang its entire MI channel.
    _SAFE_CHILD_COUNT = 1_000_000

    @classmethod
    def _parse_container_length(cls, value_str: str) -> int | None:
        """Return the container length from a GDB value string, or None.

        Handles patterns like:
          "std::vector of length 2, capacity 2"  -> 2
          "std::map with 3 elements"             -> 3
          "std::set with 5 elements"             -> 5
        Returns None if no length can be parsed, or if the value string
        contains GDB error indicators (e.g. "<error reading variable: Cannot
        access memory ...>").  In that case the varobj is backed by garbage
        memory and calling -var-update would trigger GDB internal assertion
        failures.
        """
        # If GDB embedded an error inside the value string the varobj is
        # pointing at invalid memory.  Treat it as garbage so we never call
        # -var-update on it.
        if "<error reading" in value_str or "Cannot access memory" in value_str:
            return None

        match = cls._RE_CONTAINER_LENGTH.search(value_str)
        if not match:
            return None
        # group(1): "length N" or "size N" format (vectors, deques, etc.)
        # group(2): "with N elements" format (maps, sets, unordered containers)
        if match.group(1) is not None:
            return int(match.group(1))
        return int(match.group(2))

    async def _do_var_update(self) -> list[dict]:
        """Update all tracked varobjs, returning merged changelist.

        For each dynamic (pretty-printer backed) root varobj the strategy is:

        1. Call ``-var-evaluate-expression varN`` (fast, calls only
           ``to_string()``; no children iterator; no hang risk) to get the
           current summary value and update the node label.

        2. Parse the container length from that string.  If the length is
           within a safe bound (< _SAFE_CHILD_COUNT), also call
           ``-var-update varN`` so GDB refreshes its internal varobj state
           (child count, ``new_num_children``, etc.).  This is what makes
           children correct when the user expands after the container changes.

        3. If the length looks like garbage (≥ _SAFE_CHILD_COUNT or
           unparseable AND has_more was set at creation), skip ``-var-update``
           entirely to avoid blocking GDB's MI channel for seconds.

        Child varobjs (e.g. var1.[0]) and non-dynamic varobjs are updated via
        ``-var-update *`` when no dynamic roots exist, or individually
        otherwise (because ``-var-update *`` would also re-evaluate dynamic
        roots).
        """
        if not self._varobj_names:
            return []

        if not self._var_update:
            return []

        changelist: list[dict] = []

        if not self._dynamic_varobjs:
            # Fast path: no dynamic varobjs — update everything at once.
            try:
                result = await self._var_update("*")
                changelist.extend(result)
            except Exception as exc:
                _log.warning("var_update(*) failed: %s", exc)
            return changelist

        # Slow path: at least one dynamic varobj exists.
        #
        # Step 1 + 2: per dynamic root, evaluate value string, then decide
        # whether a real -var-update is also safe.
        safe_dynamic_updated: set[str] = set()
        garbage_dynamic: set[str] = set()  # roots with garbage/huge length
        if self._var_eval_expr:
            for vo in self._dynamic_varobjs:
                try:
                    new_value = await self._var_eval_expr(vo)
                    changelist.append({"name": vo, "value": new_value})
                    _log.debug("dynamic root %s value refreshed: %r", vo, new_value)
                except Exception as exc:
                    _log.warning("var_evaluate_expression %s failed: %s", vo, exc)
                    new_value = ""

                length = self._parse_container_length(new_value)
                if length is not None and length < self._SAFE_CHILD_COUNT:
                    # Safe to call -var-update: GDB will iterate at most
                    # length elements via the pretty-printer.
                    try:
                        result = await self._var_update(vo, timeout=10.0)
                        changelist.extend(result)
                        safe_dynamic_updated.add(vo)
                        _log.debug("dynamic root %s updated (length=%d)", vo, length)
                    except Exception as exc:
                        _log.warning("var_update %s failed: %s", vo, exc)
                else:
                    # Garbage or unparseable length — also mark children as
                    # unsafe. Their varobjs were created from garbage data and
                    # sending -var-update to them can trigger GDB internal
                    # assertion failures (cplus_describe_child: Assertion
                    # 'access' failed).
                    garbage_dynamic.add(vo)
                    _log.debug(
                        "Skipping -var-update for %s and its children:"
                        " length=%s (likely garbage)",
                        vo,
                        length,
                    )

        # Step 3: update non-dynamic varobjs and children of dynamic roots.
        # Iterate _varobj_to_node which includes roots AND children.
        # Skip:
        #   - dynamic roots (handled above)
        #   - children of garbage dynamic roots (unsafe — can crash GDB)
        #   - children of fully-updated dynamic roots (already covered)
        safe_to_update: list[str] = []
        for vo in self._varobj_to_node:
            if vo in self._dynamic_varobjs:
                continue
            skip = False
            for dyn_root in safe_dynamic_updated:
                if vo.startswith(dyn_root + "."):
                    skip = True
                    break
            if not skip:
                for dyn_root in garbage_dynamic:
                    if vo.startswith(dyn_root + "."):
                        _log.debug("Skipping garbage child varobj %s in update", vo)
                        skip = True
                        break
            if skip:
                continue
            safe_to_update.append(vo)

        for vo in safe_to_update:
            try:
                result = await self._var_update(vo, timeout=10.0)
                changelist.extend(result)
            except Exception as exc:
                err = str(exc)
                if "not found" in err.lower() or "usage" in err.lower():
                    # Varobj was invalidated by GDB (e.g. parent was
                    # re-created after a type change or scope transition).
                    # Purge it and all its children so we don't keep
                    # hammering GDB with stale names.
                    _log.debug("Purging stale varobj %s: %s", vo, exc)
                    self._purge_varobj_subtree(vo)
                else:
                    _log.warning("Skipped varobj %s during update: %s", vo, exc)

        return changelist

    async def _update_variables(
        self,
        gen: int,
        frame: Frame | None,
        variables: list[LocalVariable],
    ) -> None:
        """Incremental update: keep unchanged variables as-is, handle changes."""
        if self._rebuild_gen != gen:
            return

        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        # ── 1. Evaluate addresses for innermost binding of each unique name ─
        addrs: dict[str, str] = {}
        if self._var_eval:
            for var in variables:
                if var.name in addrs:
                    continue
                if self._rebuild_gen != gen:
                    return
                try:
                    addrs[var.name] = await self._var_eval(f"&{var.name}")
                except Exception:
                    addrs[var.name] = var.type  # fallback: type string
        else:
            for var in variables:
                if var.name not in addrs:
                    addrs[var.name] = var.type

        if self._rebuild_gen != gen:
            return

        # ── 1b. Fetch DWARF declaration lines and filter out variables that
        #         haven't been initialized yet (current line ≤ decl line).
        #
        # GDB stops BEFORE executing the indicated line. So if the program is
        # stopped at line N, lines 1..N-1 have already run. A variable declared
        # on line D has had its constructor run only if N > D.
        #
        # We only filter non-argument variables: function arguments are always
        # valid once the function is entered.
        if frame:
            current_line = frame.line
        else:
            current_line = 0
        if current_line > 0 and self._get_decl_lines:
            try:
                decl_lines = await self._get_decl_lines()
            except Exception as exc:
                _log.debug("get_decl_lines failed: %s", exc)
                decl_lines = {}
            if decl_lines:
                filtered: list[LocalVariable] = []
                for var in variables:
                    if var.is_arg:
                        filtered.append(var)
                        continue
                    decl = decl_lines.get(var.name, 0)
                    if decl > 0 and current_line <= decl:
                        _log.debug(
                            "Hiding uninitialized var %s (decl=%d current=%d)",
                            var.name,
                            decl,
                            current_line,
                        )
                    else:
                        filtered.append(var)
                variables = filtered

        if self._rebuild_gen != gen:
            return

        # ── 2. Build complete binding list ─────────────────────────────────
        # GDB returns variables innermost-first; duplicate names are outer
        # (shadowed) bindings.  For outer bindings we need their address —
        # &name always returns the innermost, so we look them up from the
        # addresses already stored as keys in _tracked.
        tracked_outer: dict[str, list[str]] = {}
        for (tname, taddr) in self._tracked:
            if addrs.get(tname) != taddr:
                tracked_outer.setdefault(tname, []).append(taddr)

        new_bindings: list[tuple[str, str, LocalVariable]] = []
        seen_names: set[str] = set()
        for var in variables:
            if var.name not in seen_names:
                seen_names.add(var.name)
                new_bindings.append((var.name, addrs.get(var.name, var.type), var))
            else:
                outer_addrs = tracked_outer.get(var.name, [])
                if outer_addrs:
                    outer_addr = outer_addrs.pop(0)
                    new_bindings.append((var.name, outer_addr, var))
                # else: unknown outer binding address (only when attaching
                # mid-execution inside an inner scope we've never seen) — skip.

        # ── 3. Determine shadow status ─────────────────────────────────────
        # A binding (name, addr) is shadowed iff addr != innermost addr for
        # that name, i.e. there exists a more-inner binding with the same name.
        new_binding_keys: set[tuple[str, str]] = set()
        shadowed_keys: set[tuple[str, str]] = set()
        for bname, baddr, _ in new_bindings:
            key = (bname, baddr)
            new_binding_keys.add(key)
            if addrs.get(bname) != baddr:
                shadowed_keys.add(key)

        # ── 4. Compute new frame key ───────────────────────────────────────
        new_frame_key: tuple | None = None
        if frame and frame.func:
            var_sig = frozenset(new_binding_keys)
            new_frame_key = (frame.func, frame.fullname or frame.file, var_sig)

        # ── 5. Classify changes ────────────────────────────────────────────
        current_keys = set(self._tracked.keys())

        # Discard placeholder nodes for variables that left scope.
        for key in list(self._uninitialized_nodes.keys()):
            if key not in new_binding_keys:
                self._uninitialized_nodes.pop(key).remove()

        to_remove: list[tuple[str, str]] = []
        for k in current_keys:
            if k not in new_binding_keys:
                to_remove.append(k)

        to_add: list[tuple[str, str, LocalVariable]] = []
        for n, a, v in new_bindings:
            if (n, a) not in current_keys:
                to_add.append((n, a, v))
        # to_reanchor: already tracked with a floating varobj, but now
        # shadowed by an inner-scope binding.  Must replace with a pinned
        # *(Type*)addr varobj so GDB stops hijacking it.
        to_reanchor: list[tuple[str, str, LocalVariable]] = []
        for bname, baddr, bvar in new_bindings:
            key = (bname, baddr)
            if key not in current_keys:
                continue
            if key not in shadowed_keys:
                continue
            varobj = self._tracked.get(key, "")
            if varobj and varobj not in self._pinned_varobjs:
                to_reanchor.append((bname, baddr, bvar))

        # ── 6. Fast path: nothing structural changed ───────────────────────
        no_change = (
            not to_remove
            and not to_reanchor
            and not to_add
            and new_frame_key == self._frame_key
        )
        if no_change:
            try:
                changelist = await self._do_var_update()
                if self._rebuild_gen == gen:
                    self._apply_changelist(changelist)
            except Exception:
                pass
            self._sync_shadow_labels(shadowed_keys)
            return

        # ── 7. Save expansion for outgoing frame key ───────────────────────
        if self._frame_key is not None and self._frame_key != new_frame_key:
            paths = self._collect_expanded_paths()
            if paths:
                self._saved_expansions[self._frame_key] = paths
        self._frame_key = new_frame_key

        # ── 8. Update values for UNCHANGED variables ───────────────────────
        stale_varobjs: set[str] = set()
        for rname, raddr in to_remove:
            vo = self._tracked.get((rname, raddr), "")
            if vo:
                stale_varobjs.add(vo)
        for rname, raddr, _ in to_reanchor:
            vo = self._tracked.get((rname, raddr), "")
            if vo:
                stale_varobjs.add(vo)
        if self._varobj_names:
            try:
                changelist = await self._do_var_update()
                if self._rebuild_gen == gen:
                    self._apply_changelist(changelist, skip_varobjs=stale_varobjs)
            except Exception:
                pass

        if self._rebuild_gen != gen:
            return

        # ── 9. Re-anchor variables that became shadowed ────────────────────
        for bname, baddr, bvar in to_reanchor:
            await self._reanchor_var(gen, bname, baddr, bvar, tree)
            if self._rebuild_gen != gen:
                return

        # ── 10. Remove gone variables ──────────────────────────────────────
        for rname, raddr in to_remove:
            varobj = self._tracked.pop((rname, raddr), "")
            if varobj:
                node = self._varobj_to_node.get(varobj)
            else:
                node = None
            if varobj:
                self._purge_varobj_subtree(varobj)
                self._pinned_varobjs.discard(varobj)
            if node is not None:
                node.remove()
            if varobj:
                try:
                    self._varobj_names.remove(varobj)
                except ValueError:
                    pass
            if varobj and self._var_delete:
                try:
                    await self._var_delete(varobj)
                except Exception:
                    pass
            if self._rebuild_gen != gen:
                return

        # ── 11. Add new variables ──────────────────────────────────────────
        if new_frame_key:
            restore = self._saved_expansions.get(new_frame_key, set())
        else:
            restore = set()
        if self._var_create:
            for bname, baddr, bvar in to_add:
                if self._rebuild_gen != gen:
                    return
                # Remove the placeholder node left by a previous uninitialized
                # state so we don't end up with a duplicate entry.
                if (bname, baddr) in self._uninitialized_nodes:
                    self._uninitialized_nodes.pop((bname, baddr)).remove()
                try:
                    info = await self._var_create(bvar.name)
                except Exception:
                    if bvar.value:
                        val = bvar.value.replace("\n", " ")
                    else:
                        val = "<complex>"
                    tree.root.add_leaf(f"{bvar.name} = {val}")
                    self._tracked[(bname, baddr)] = ""
                    continue

                if self._rebuild_gen != gen:
                    return

                varobj_name = info.get("name", "")
                value = info.get("value", "")

                # If GDB's value contains an error the variable's storage is
                # not yet accessible — it is in scope but not yet initialized
                # (e.g. a std::map declared with a multi-line brace initializer:
                # GDB sees the name from the opening { of main before the
                # constructor runs).  Delete the bad varobj immediately so its
                # corrupted internal state never reaches -var-update or
                # -var-list-children (which would crash GDB).  Record a
                # placeholder node and leave _tracked empty for this key so
                # the next stop's rebuild retries var_create automatically.
                if varobj_name and (
                    "<error reading" in value
                    or "Cannot access memory" in value
                ):
                    if self._var_delete:
                        try:
                            await self._var_delete(varobj_name)
                        except Exception:
                            pass
                    shadow_suffix = "  ← shadowed" if (bname, baddr) in shadowed_keys else ""
                    placeholder = tree.root.add_leaf(
                        f"{bvar.name} = <not yet initialized>{shadow_suffix}",
                        data={"varobj": "", "exp": bvar.name, "has_children": False, "displayhint": ""},
                    )
                    self._uninitialized_nodes[(bname, baddr)] = placeholder
                    _log.debug(
                        "var_create %s: uninitialized memory (%r), placeholder added",
                        bvar.name, value,
                    )
                    continue

                if varobj_name:
                    self._varobj_names.append(varobj_name)
                    self._tracked[(bname, baddr)] = varobj_name
                    if info.get("dynamic", "0") == "1":
                        self._dynamic_varobjs.add(varobj_name)
                    type_str = info.get("type", "")
                    if type_str:
                        self._varobj_type[varobj_name] = type_str
                else:
                    self._tracked[(bname, baddr)] = ""

                numchild = self._safe_int(info.get("numchild", "0"))
                has_children = (
                    numchild > 0 or info.get("dynamic", "0") == "1"
                ) and not _suppress_children(info)
                dh = info.get("displayhint", "")

                # New bindings from to_add are always innermost (non-shadowed)
                # in normal stepping flow; add shadow suffix defensively if not.
                if (bname, baddr) in shadowed_keys:
                    shadow_suffix = "  ← shadowed"
                else:
                    shadow_suffix = ""

                if has_children:
                    label = bvar.name
                    if value:
                        label += f" = {self._truncate(value)}"
                    node = tree.root.add(
                        label + shadow_suffix,
                        expand=False,
                        data={
                            "varobj": varobj_name,
                            "exp": bvar.name,
                            "loaded": False,
                            "has_children": True,
                            "displayhint": dh,
                        },
                    )
                    node.add_leaf("⏳ loading...")
                else:
                    label = f"{bvar.name} = {value}"
                    node = tree.root.add_leaf(
                        label + shadow_suffix,
                        data={
                            "varobj": varobj_name,
                            "exp": bvar.name,
                            "has_children": False,
                            "displayhint": dh,
                        },
                    )

                if varobj_name:
                    self._varobj_to_node[varobj_name] = node

                if restore:
                    paths_for_name = []
                    for p in restore:
                        if p and p[0] == bname:
                            paths_for_name.append(p)
                    paths_for_name.sort(key=len)
                    for path in paths_for_name:
                        if self._rebuild_gen != gen:
                            return
                        await self._restore_expansion(tree.root, path, gen)

        # ── 12. Sync shadow labels on all surviving nodes ──────────────────
        self._sync_shadow_labels(shadowed_keys)

    # ------------------------------------------------------------------
    # Shadow re-anchor
    # ------------------------------------------------------------------

    async def _reanchor_var(
        self,
        gen: int,
        name: str,
        addr: str,
        outer_var: LocalVariable,
        tree: Tree,
    ) -> None:
        """Replace the floating varobj for (name, addr) with an address-pinned one.

        Called when a new inner-scope variable with the same name shadows this
        outer binding.  GDB would otherwise hijack the floating varobj to
        follow the inner binding.  The original tree node is kept so expansion
        state is preserved.  The label gets a "← shadowed" suffix.
        """
        key = (name, addr)
        old_varobj = self._tracked.get(key, "")

        # Grab the existing tree node BEFORE any cleanup.
        if old_varobj:
            node = self._varobj_to_node.get(old_varobj)
        else:
            node = None

        # Save type BEFORE purging — _purge_varobj_subtree removes _varobj_type entries.
        # Type comes from the var_create response; outer_var.type is always empty
        # because -stack-list-variables never returns a type field.
        if old_varobj:
            type_str = self._varobj_type.get(old_varobj, "")
        else:
            type_str = ""

        # Purge tracking entries for old varobj and its children.
        if old_varobj:
            self._purge_varobj_subtree(old_varobj)
            try:
                self._varobj_names.remove(old_varobj)
            except ValueError:
                pass
            self._pinned_varobjs.discard(old_varobj)
            if self._var_delete:
                try:
                    await self._var_delete(old_varobj)
                except Exception:
                    pass

        # Clear tracked entry; will be re-set if pinning succeeds.
        self._tracked.pop(key, None)

        if self._rebuild_gen != gen:
            return

        # Build *(Type*)addr expression.
        if not type_str:
            # No type — show as non-expandable leaf with last known value.
            if node is not None:
                if outer_var.value:
                    val = self._compact_value(outer_var.value)
                else:
                    val = "?"
                node.set_label(f"{name} = {val}  ← shadowed")
                node.data["has_children"] = False
            return

        addr_expr = f"*({type_str}*){addr}"
        try:
            info = await self._var_create(addr_expr)
        except Exception:
            if node is not None:
                if outer_var.value:
                    val = self._compact_value(outer_var.value)
                else:
                    val = "?"
                node.set_label(f"{name} = {val}  ← shadowed")
                node.data["has_children"] = False
            return

        if self._rebuild_gen != gen:
            return

        new_varobj = info.get("name", "")
        self._tracked[key] = new_varobj
        if new_varobj:
            self._varobj_names.append(new_varobj)
            self._pinned_varobjs.add(new_varobj)
            if info.get("dynamic", "0") == "1":
                self._dynamic_varobjs.add(new_varobj)
            # Save type for further re-anchoring (e.g. a third binding appears).
            reanchor_type = info.get("type", "") or type_str
            if reanchor_type:
                self._varobj_type[new_varobj] = reanchor_type

        value = info.get("value", outer_var.value or "")
        numchild = self._safe_int(info.get("numchild", "0"))
        has_children = (
            numchild > 0 or info.get("dynamic", "0") == "1"
        ) and not _suppress_children(info)

        if node is not None:
            node.data["varobj"] = new_varobj
            node.data["has_children"] = has_children
            if new_varobj:
                self._varobj_to_node[new_varobj] = node

            exp = node.data.get("exp", name)
            if has_children:
                label = exp
                if value:
                    label += f" = {self._compact_value(value)}"
            else:
                if value:
                    label = f"{exp} = {value}"
                else:
                    label = exp
            node.set_label(label + "  ← shadowed")

            # If the node was expanded and had loaded children, reload them
            # under the new varobj name (old children varobjs are now gone).
            if node.data.get("loaded") and has_children and new_varobj:
                node.data["loaded"] = False
                node.remove_children()
                node.add_leaf("⏳ loading...")
                asyncio.create_task(self._load_children(node, new_varobj))
        else:
            # No existing node — create a fresh one.
            dh = info.get("displayhint", "")
            if has_children:
                label = name
                if value:
                    label += f" = {self._compact_value(value)}"
                node_new = tree.root.add(
                    label + "  ← shadowed",
                    expand=False,
                    data={
                        "varobj": new_varobj,
                        "exp": name,
                        "loaded": False,
                        "has_children": True,
                        "displayhint": dh,
                    },
                )
                node_new.add_leaf("⏳ loading...")
            else:
                if value:
                    label = f"{name} = {value}"
                else:
                    label = name
                node_new = tree.root.add_leaf(
                    label + "  ← shadowed",
                    data={
                        "varobj": new_varobj,
                        "exp": name,
                        "has_children": False,
                        "displayhint": dh,
                    },
                )
            if new_varobj:
                self._varobj_to_node[new_varobj] = node_new

    def _sync_shadow_labels(self, shadowed_keys: set[tuple[str, str]]) -> None:
        """Add or remove '  ← shadowed' suffix on all tracked nodes.

        Called at the end of every update cycle.  Handles the common case
        where the inner scope exits: the outer binding's (name, addr) key is
        no longer in shadowed_keys so its label gets the suffix stripped.
        """
        for (name, addr), varobj in self._tracked.items():
            if varobj:
                node = self._varobj_to_node.get(varobj)
            else:
                node = None
            if node is None:
                continue
            if hasattr(node.label, "plain"):
                label_plain = node.label.plain
            else:
                label_plain = str(node.label)
            is_currently_shadowed = label_plain.endswith("  ← shadowed")
            should_be_shadowed = (name, addr) in shadowed_keys
            if should_be_shadowed == is_currently_shadowed:
                continue
            if should_be_shadowed:
                node.set_label(label_plain + "  ← shadowed")
            else:
                node.set_label(label_plain[: -len("  ← shadowed")])

    # ------------------------------------------------------------------
    # Value update helper
    # ------------------------------------------------------------------

    def _apply_changelist(
        self,
        changelist: list[dict],
        skip_varobjs: "set[str]" = frozenset(),
    ) -> None:
        """Apply -var-update changelist results to existing tree nodes.

        Hidden children (those not yet fetched due to expandchildlimit) are
        never in *changelist* because GDB only knows about varobjs that were
        explicitly created via -var-list-children.  No special handling is
        needed — they are naturally skipped.
        """
        _log.debug("changelist: %d changes", len(changelist))
        for change in changelist:
            varobj_name = change.get("name", "")
            if varobj_name in skip_varobjs:
                continue
            in_scope = change.get("in_scope", "true")
            type_changed = change.get("type_changed", "false") == "true"
            if in_scope != "true" or type_changed:
                continue  # stale / changed — handled by to_remove/to_add
            node = self._varobj_to_node.get(varobj_name)
            if node is None:
                continue
            data = node.data
            if not isinstance(data, dict):
                continue
            # Sentinel "load more" nodes should never appear in the changelist
            # (they are not registered in _varobj_to_node), but guard anyway.
            if data.get("load_more"):
                continue
            new_value = change.get("value", "")
            exp = data.get("exp", "")
            has_children = data.get("has_children", False)
            is_pinned = varobj_name in self._pinned_varobjs
            if has_children:
                label = exp
                if new_value:
                    if is_pinned:
                        label += f" = {self._compact_value(new_value)}"
                    else:
                        label += f" = {self._truncate(new_value)}"
            else:
                if new_value:
                    label = f"{exp} = {new_value}"
                else:
                    label = exp
            if is_pinned:
                label += "  ← shadowed"
            node.set_label(label)
            new_num_children = change.get("new_num_children")
            if new_num_children is not None and data.get("loaded"):
                # The child count changed (e.g. vector grew or shrank).
                # Reset the node so it reloads its children fresh.
                data["loaded"] = False
                node.remove_children()
                if node.is_expanded:
                    asyncio.create_task(self._load_children(node, varobj_name))
                else:
                    node.add_leaf("⏳ loading...")

    # ------------------------------------------------------------------
    # Expansion save / restore helpers
    # ------------------------------------------------------------------

    def _collect_expanded_paths(self) -> set[tuple[str, ...]]:
        """Walk the live tree and return paths of all expanded nodes."""
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
                    walk(child, child_path)

        walk(tree.root, ())
        return paths

    async def _ensure_children_loaded(self, node: TreeNode) -> bool:
        """Load node children from GDB on demand if not yet fetched."""
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
        dh = data.get("displayhint", "")
        try:
            children, has_more = await self._var_list_children(
                varobj, limit=self._cfg.expandchildlimit
            )
            if children:
                await self._add_children(node, children, dh)
                if has_more:
                    self._add_load_more_node(node, varobj, len(children), dh)
            else:
                node.add_leaf("(empty)")
            return bool(children)
        except Exception as e:
            data["loaded"] = False
            _log.warning("failed to load children for %s: %s", varobj, e)
            node.add_leaf(f"⚠ {e}")
            return False

    async def _restore_expansion(
        self, node: TreeNode, path: tuple[str, ...], gen: int
    ) -> None:
        """Expand the node at *path*, loading children on demand at each level."""
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
    # Lazy child loading
    # ------------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data
        if not isinstance(data, dict):
            return
        if data.get("loaded"):
            return
        data["loaded"] = True
        if data.get("load_more"):
            varobj = data.get("varobj", "")
            from_idx = data.get("from_idx", 0)
            parent_dh = data.get("displayhint", "")
            if varobj and self._var_list_children:
                asyncio.create_task(
                    self._load_more_children(node, varobj, from_idx, parent_dh)
                )
            return
        varobj = data.get("varobj", "")
        if not varobj or not self._var_list_children:
            return
        asyncio.create_task(self._load_children(node, varobj))

    async def _load_children(self, node: TreeNode, varobj_name: str) -> None:
        # Purge stale child entries from previous load before refreshing.
        prefix = varobj_name + "."
        stale_children = []
        for k in self._varobj_to_node:
            if k.startswith(prefix):
                stale_children.append(k)
        for k in stale_children:
            self._varobj_to_node.pop(k, None)
            self._dynamic_varobjs.discard(k)
        node.remove_children()
        try:
            children, has_more = await self._var_list_children(
                varobj_name, limit=self._cfg.expandchildlimit
            )
        except Exception as e:
            node.add_leaf(f"⚠ {e}")
            return
        if not children:
            node.add_leaf("(empty)")
            return
        _log.debug(
            "load_children varobj=%s -> %d children has_more=%s",
            varobj_name,
            len(children),
            has_more,
        )
        # Pass the parent node's displayhint so _add_children knows how to
        # lay out the children (e.g. "map" → pair key-value, "array" → index).
        if isinstance(node.data, dict):
            parent_dh = node.data.get("displayhint", "")
        else:
            parent_dh = ""
        await self._add_children(node, children, parent_dh)
        if has_more:
            self._add_load_more_node(node, varobj_name, len(children), parent_dh)

    def _add_load_more_node(
        self,
        parent: TreeNode,
        varobj_name: str,
        from_idx: int,
        parent_dh: str,
    ) -> None:
        """Add an expandable sentinel node that fetches the next batch on expand."""
        limit = self._cfg.expandchildlimit
        if limit > 0:
            label = f"load more items [{from_idx} shown]"
        else:
            label = f"load remaining items [{from_idx} shown]"
        sentinel = parent.add(
            label,
            expand=False,
            data={
                "load_more": True,
                "loaded": False,
                "varobj": varobj_name,
                "from_idx": from_idx,
                "displayhint": parent_dh,
            },
        )
        # A placeholder child is required for Textual to render the expand triangle.
        sentinel.add_leaf("")

    async def _load_more_children(
        self,
        sentinel: TreeNode,
        varobj_name: str,
        from_idx: int,
        parent_dh: str,
    ) -> None:
        """Fetch the next batch of children and append them as siblings of the sentinel."""
        parent = sentinel.parent
        # Remove the sentinel first so it does not linger in the tree.
        sentinel.remove()
        if parent is None:
            return
        try:
            children, has_more = await self._var_list_children(
                varobj_name, from_idx, limit=self._cfg.expandchildlimit
            )
        except Exception as e:
            parent.add_leaf(f"⚠ {e}")
            return
        _log.debug(
            "load_more_children varobj=%s from=%d -> %d children",
            varobj_name,
            from_idx,
            len(children),
        )
        if children:
            await self._add_children(parent, children, parent_dh)
        if has_more:
            next_idx = from_idx + len(children)
            self._add_load_more_node(parent, varobj_name, next_idx, parent_dh)

    async def _add_children(
        self,
        node: TreeNode,
        children: list[dict],
        displayhint: str = "",
    ) -> None:
        """Add child nodes to *node*.

        *displayhint* is the GDB pretty-printer hint on the **parent** varobj
        and controls how children are laid out:

        * ``"map"`` — children arrive as alternating key/value pairs
          (``[0]``=key, ``[1]``=value, …).  Pair them as ``[key_val]=val``.
        * ``"array"`` / ``""`` / anything else — show each child individually
          with its ``exp`` label, flattening access-specifier pseudo-nodes
          (``public`` / ``private`` / ``protected``) inline.
        """
        if displayhint == "map":
            # GDB sends alternating key/value children.  Pair them.
            it = iter(children)
            for key_child in it:
                try:
                    val_child = next(it)
                except StopIteration:
                    break
                key_val = key_child.get("value", "?")
                val_name = val_child.get("name", "")
                val_numchild = self._safe_int(val_child.get("numchild", "0"))
                val_dynamic = val_child.get("dynamic", "0") == "1"
                val_has_children = (
                    val_numchild > 0 or val_dynamic
                ) and not _suppress_children(val_child)
                val_value = val_child.get("value", "")
                val_dh = val_child.get("displayhint", "")
                exp = f"[{key_val}]"
                if val_has_children:
                    label = exp
                    if val_value:
                        label += f" = {self._truncate(val_value)}"
                    child_node = node.add(
                        label,
                        expand=False,
                        data={
                            "varobj": val_name,
                            "exp": exp,
                            "loaded": False,
                            "has_children": True,
                            "displayhint": val_dh,
                        },
                    )
                    child_node.add_leaf("⏳ loading...")
                else:
                    if val_value:
                        label = f"{exp} = {val_value}"
                    else:
                        label = exp
                    child_node = node.add_leaf(
                        label,
                        data={
                            "varobj": val_name,
                            "exp": exp,
                            "has_children": False,
                            "displayhint": val_dh,
                        },
                    )
                if val_name:
                    self._varobj_to_node[val_name] = child_node
                    if val_dynamic:
                        self._dynamic_varobjs.add(val_name)
            return

        # Default: individual children, with access-specifier flattening.
        _ACCESS = {"public", "private", "protected"}
        for child in children:
            child_name = child.get("name", "")
            exp = child.get("exp", "")
            numchild = self._safe_int(child.get("numchild", "0"))
            dynamic = child.get("dynamic", "0") == "1"
            has_children = (numchild > 0 or dynamic) and not _suppress_children(child)
            value = child.get("value", "")
            child_dh = child.get("displayhint", "")

            if exp in _ACCESS and has_children:
                try:
                    grandchildren, more = await self._var_list_children(
                        child_name, limit=self._cfg.expandchildlimit
                    )
                    await self._add_children(node, grandchildren)
                    if more:
                        # The access-specifier block has more members than
                        # expandchildlimit — add a "load more" sentinel so the
                        # user can page through the remaining siblings.
                        self._add_load_more_node(
                            node, child_name, len(grandchildren), ""
                        )
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
                    data={
                        "varobj": child_name,
                        "exp": exp,
                        "loaded": False,
                        "has_children": True,
                        "displayhint": child_dh,
                    },
                )
                child_node.add_leaf("⏳ loading...")
            else:
                if value:
                    label = f"{exp} = {value}"
                else:
                    label = exp
                child_node = node.add_leaf(
                    label,
                    data={
                        "varobj": child_name,
                        "exp": exp,
                        "has_children": False,
                        "displayhint": child_dh,
                    },
                )

            if child_name:
                self._varobj_to_node[child_name] = child_node
                if dynamic:
                    self._dynamic_varobjs.add(child_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _recreate_dynamic_varobj(self, vo: str) -> str:
        # No longer used — kept as stub to avoid merge noise.
        return ""

    def _purge_varobj_subtree(self, varobj_name: str) -> None:
        """Remove *varobj_name* and all its child varobjs from tracking dicts.

        Must be called whenever a GDB varobj is deleted so stale child
        entries don't accumulate in _varobj_to_node and _dynamic_varobjs
        and trigger spurious -var-update calls (which can crash GDB with
        "cplus_describe_child: Assertion 'access' failed").
        """
        prefix = varobj_name + "."
        stale = []
        for k in self._varobj_to_node:
            if k == varobj_name or k.startswith(prefix):
                stale.append(k)
        for k in stale:
            self._varobj_to_node.pop(k, None)
            self._dynamic_varobjs.discard(k)
            self._varobj_type.pop(k, None)

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

    @staticmethod
    def _compact_value(s: str) -> str:
        """Collapse compound-type values (those starting with '{') to '{...}'.

        Used for shadowed-variable labels where the full value would be very
        long and the expanded tree node already shows all the details.
        Primitive values (int, float, string, …) are returned unchanged.

        Note: for regular (non-shadowed) collapsed compound variables the
        label already shows ``w = {...}`` because GDB's ``-var-create``
        returns ``{...}`` as the value summary when pretty-printing is
        enabled.  This helper is not involved in that path.
        """
        if s.strip().startswith("{"):
            return "{...}"
        return s
