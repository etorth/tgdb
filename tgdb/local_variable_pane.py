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
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from .gdb_controller import LocalVariable, Frame
from .highlight_groups import HighlightGroups
from .pane_base import PaneBase


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

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._variables: list[LocalVariable] = []

        # Async callbacks wired up by the app
        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        self._var_update: Optional[Callable[..., Coroutine]] = None
        self._var_eval: Optional[Callable[..., Coroutine]] = None

        # Per-variable state for incremental updates.
        # name → (varobj_name, address)  — innermost binding only.
        self._tracked: dict[str, tuple[str, str]] = {}

        # varobj name → TreeNode (all depths)
        self._varobj_to_node: dict[str, TreeNode] = {}

        # All live GDB varobj names (for cleanup)
        self._varobj_names: list[str] = []

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
    ) -> None:
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete
        self._var_update = var_update
        self._var_eval = var_eval

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

        # ── 1. Evaluate addresses (unique names only — innermost binding) ─
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

        # ── 2. Separate innermost bindings from shadowed outer ones ────────
        # GDB returns multiple entries for the same name when scopes shadow
        # each other (innermost first).  We create varobjs only for the first
        # (innermost) occurrence; outer ones become non-expandable leaf nodes.
        new_main: list[tuple[str, str, LocalVariable]] = []  # (name, addr, var)
        new_shadowed: list[LocalVariable] = []
        seen: set[str] = set()
        for var in variables:
            if var.name not in seen:
                seen.add(var.name)
                new_main.append((var.name, addrs.get(var.name, var.type), var))
            else:
                new_shadowed.append(var)

        # ── 3. Compute new frame key ───────────────────────────────────────
        new_frame_key: tuple | None = None
        if frame and frame.func:
            var_sig = frozenset((name, addr) for name, addr, _ in new_main)
            new_frame_key = (frame.func, frame.fullname or frame.file, var_sig)

        # ── 4. Determine per-variable changes ─────────────────────────────
        to_remove: list[tuple[str, str]] = []   # (name, varobj_name) gone/changed
        to_add: list[tuple[str, str, LocalVariable]] = []  # (name, addr, var) new/changed

        for name, (old_varobj, old_addr) in self._tracked.items():
            # Still present with same address → unchanged
            if any(n == name and a == old_addr for n, a, _ in new_main):
                continue
            to_remove.append((name, old_varobj))

        for name, addr, var in new_main:
            old = self._tracked.get(name)
            if old is None or old[1] != addr:
                to_add.append((name, addr, var))

        # ── 5. Fast path: nothing structural changed ───────────────────────
        if not to_remove and not to_add and new_frame_key == self._frame_key:
            # Rebuild only the shadowed-leaf section (values may have changed)
            await self._refresh_shadow_leaves(gen, tree, new_shadowed)
            if self._var_update:
                try:
                    changelist = await self._var_update("*")
                    if self._rebuild_gen == gen:
                        self._apply_changelist(changelist)
                except Exception:
                    pass
            return

        # ── 6. Save expansion for outgoing frame key ───────────────────────
        # Collect BEFORE removing any nodes so expanded state is captured.
        if self._frame_key is not None and self._frame_key != new_frame_key:
            paths = self._collect_expanded_paths()
            if paths:
                self._saved_expansions[self._frame_key] = paths
        self._frame_key = new_frame_key

        # ── 7. Update values for UNCHANGED variables via var_update ────────
        stale_varobjs = {vo for _, vo in to_remove}
        if self._var_update and self._varobj_names:
            try:
                changelist = await self._var_update("*")
                if self._rebuild_gen == gen:
                    self._apply_changelist(changelist, skip_varobjs=stale_varobjs)
            except Exception:
                pass

        if self._rebuild_gen != gen:
            return

        # ── 8. Remove stale variables ──────────────────────────────────────
        for name, varobj_name in to_remove:
            del self._tracked[name]
            node = self._varobj_to_node.pop(varobj_name, None)
            if node is not None:
                node.remove()
            try:
                self._varobj_names.remove(varobj_name)
            except ValueError:
                pass
            if self._var_delete:
                try:
                    await self._var_delete(varobj_name)
                except Exception:
                    pass
            if self._rebuild_gen != gen:
                return

        # ── 9. Refresh shadowed leaf nodes ─────────────────────────────────
        await self._refresh_shadow_leaves(gen, tree, new_shadowed)
        if self._rebuild_gen != gen:
            return

        # ── 10. Restore paths for newly added variables ────────────────────
        restore = self._saved_expansions.get(new_frame_key, set()) if new_frame_key else set()

        # ── 11. Add new variables ──────────────────────────────────────────
        if self._var_create:
            for name, addr, var in to_add:
                if self._rebuild_gen != gen:
                    return
                try:
                    info = await self._var_create(var.name)
                except Exception:
                    val = var.value.replace("\n", " ") if var.value else "<complex>"
                    tree.root.add_leaf(f"{var.name} = {val}")
                    self._tracked[name] = ("", addr)
                    continue

                if self._rebuild_gen != gen:
                    return

                varobj_name = info.get("name", "")
                if varobj_name:
                    self._varobj_names.append(varobj_name)
                    self._tracked[name] = (varobj_name, addr)

                numchild = self._safe_int(info.get("numchild", "0"))
                has_children = numchild > 0 or info.get("dynamic", "0") == "1"
                value = info.get("value", "")

                if has_children:
                    label = var.name
                    if value:
                        label += f" = {self._truncate(value)}"
                    node = tree.root.add(
                        label,
                        expand=False,
                        data={"varobj": varobj_name, "exp": var.name,
                              "loaded": False, "has_children": True},
                    )
                    node.add_leaf("⏳ loading...")
                else:
                    label = f"{var.name} = {value}"
                    node = tree.root.add_leaf(
                        label,
                        data={"varobj": varobj_name, "exp": var.name,
                              "has_children": False},
                    )

                if varobj_name:
                    self._varobj_to_node[varobj_name] = node

                # Restore expansion for this variable from saved state.
                if restore:
                    paths_for_name = sorted(
                        (p for p in restore if p and p[0] == name),
                        key=len,
                    )
                    for path in paths_for_name:
                        if self._rebuild_gen != gen:
                            return
                        await self._restore_expansion(tree.root, path, gen)

    # ------------------------------------------------------------------
    # Shadowed leaf management
    # ------------------------------------------------------------------

    async def _refresh_shadow_leaves(
        self,
        gen: int,
        tree: Tree,
        shadowed: list[LocalVariable],
    ) -> None:
        """Remove old and (re)add current shadowed-variable leaf nodes."""
        if self._rebuild_gen != gen:
            return
        # Remove any existing shadow leaves.
        for child in list(tree.root.children):
            data = child.data
            if isinstance(data, dict) and data.get("shadow"):
                child.remove()
        # Add current shadowed variables.
        for var in shadowed:
            val = var.value.replace("\n", " ") if var.value else "?"
            label = f"{var.name}: {var.type} = {val}  ← outer scope"
            tree.root.add_leaf(label, data={"shadow": True, "exp": var.name})

    # ------------------------------------------------------------------
    # Value update helper
    # ------------------------------------------------------------------

    def _apply_changelist(
        self,
        changelist: list[dict],
        skip_varobjs: "set[str]" = frozenset(),
    ) -> None:
        """Apply -var-update changelist results to existing tree nodes."""
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
            new_value = change.get("value", "")
            exp = data.get("exp", "")
            has_children = data.get("has_children", False)
            if has_children:
                label = exp
                if new_value:
                    label += f" = {self._truncate(new_value)}"
            else:
                label = f"{exp} = {new_value}" if new_value else exp
            node.set_label(label)
            new_num_children = change.get("new_num_children")
            if new_num_children is not None and data.get("loaded"):
                data["loaded"] = False
                node.remove_children()
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
                if data.get("shadow"):
                    continue  # shadow leaves are not part of expansion state
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
        try:
            children = await self._var_list_children(varobj)
            if children:
                await self._add_children(node, children)
            else:
                node.add_leaf("(empty)")
            return bool(children)
        except Exception:
            data["loaded"] = False
            node.add_leaf("⚠ error fetching children")
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
        varobj = data.get("varobj", "")
        if not varobj or not self._var_list_children:
            return
        data["loaded"] = True
        asyncio.create_task(self._load_children(node, varobj))

    async def _load_children(self, node: TreeNode, varobj_name: str) -> None:
        node.remove_children()
        try:
            children = await self._var_list_children(varobj_name)
        except Exception:
            node.add_leaf("⚠ error fetching children")
            return
        if not children:
            node.add_leaf("(empty)")
            return
        await self._add_children(node, children)

    async def _add_children(self, node: TreeNode, children: list[dict]) -> None:
        """Add child nodes, flattening access specifiers and pairing map entries."""
        _ACCESS = {"public", "private", "protected"}

        paired = self._detect_map_pairs(children)
        if paired:
            for key_child, val_child in paired:
                key_val = key_child.get("value", "?")
                val_name = val_child.get("name", "")
                val_numchild = self._safe_int(val_child.get("numchild", "0"))
                val_dynamic = val_child.get("dynamic", "0") == "1"
                val_has_children = val_numchild > 0 or val_dynamic
                val_value = val_child.get("value", "")
                exp = f"[{key_val}]"
                if val_has_children:
                    label = exp
                    if val_value:
                        label += f" = {self._truncate(val_value)}"
                    child_node = node.add(
                        label, expand=False,
                        data={"varobj": val_name, "exp": exp,
                              "loaded": False, "has_children": True},
                    )
                    child_node.add_leaf("⏳ loading...")
                else:
                    label = f"{exp} = {val_value}" if val_value else exp
                    child_node = node.add_leaf(
                        label,
                        data={"varobj": val_name, "exp": exp, "has_children": False},
                    )
                if val_name:
                    self._varobj_to_node[val_name] = child_node
            return

        for child in children:
            child_name = child.get("name", "")
            exp = child.get("exp", "")
            numchild = self._safe_int(child.get("numchild", "0"))
            dynamic = child.get("dynamic", "0") == "1"
            has_children = numchild > 0 or dynamic
            value = child.get("value", "")

            if exp in _ACCESS and has_children:
                try:
                    grandchildren = await self._var_list_children(child_name)
                    await self._add_children(node, grandchildren)
                except Exception:
                    pass
                continue

            if has_children:
                label = exp
                if value:
                    label += f" = {self._truncate(value)}"
                child_node = node.add(
                    label, expand=False,
                    data={"varobj": child_name, "exp": exp,
                          "loaded": False, "has_children": True},
                )
                child_node.add_leaf("⏳ loading...")
            else:
                label = f"{exp} = {value}" if value else exp
                child_node = node.add_leaf(
                    label,
                    data={"varobj": child_name, "exp": exp, "has_children": False},
                )

            if child_name:
                self._varobj_to_node[child_name] = child_node

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_map_pairs(children: list[dict]) -> list[tuple[dict, dict]] | None:
        n = len(children)
        if n == 0 or n % 2 != 0:
            return None
        for i, c in enumerate(children):
            if c.get("exp", "") != str(i):
                return None
        return [(children[i], children[i + 1]) for i in range(0, n, 2)]

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
