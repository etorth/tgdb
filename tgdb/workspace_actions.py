"""Workspace and pane-management helpers for the application package."""

from dataclasses import replace
from typing import TYPE_CHECKING

from textual.widget import Widget
from textual.css.query import NoMatches

from .source_widget import SourceView
from .gdb_widget import GDBWidget
from .command_line_bar import CommandLineBar
from .file_dialog import FileDialog
from .context_menu import (
    ContextMenu,
    ContextMenuItem,
    ContextMenuClosed,
    ContextMenuSelected,
)
from .local_variable_pane import LocalVariablePane
from .register_pane import RegisterPane
from .stack_pane import StackPane
from .thread_pane import ThreadPane
from .evaluate_pane import EvaluatePane
from .memory_pane import MemoryPane
from .disasm_pane import DisasmPane
from .workspace import EmptyPane, PaneContainer, Splitter
from .async_util import supervise

if TYPE_CHECKING:
    from .main import TGDBApp


class WorkspaceMixin:
    """Dynamic workspace / pane management."""

    # ------------------------------------------------------------------
    # Pane factories
    # ------------------------------------------------------------------

    def _make_source_pane(self: "TGDBApp") -> SourceView:
        if self._source_view is None:
            self._source_view = SourceView(self.hl, id="src-pane")
        return self._source_view


    def _make_gdb_pane(self: "TGDBApp") -> GDBWidget:
        if self._gdb_widget is None:
            self._gdb_widget = GDBWidget(
                self.hl,
                max_scrollback=self.cfg.scrollbackbuffersize,
                id="gdb-pane",
            )
        return self._gdb_widget


    def _make_local_variable_pane(self: "TGDBApp") -> LocalVariablePane:
        if self._locals_pane is None:
            self._locals_pane = LocalVariablePane(self.hl, self.cfg)
            self._locals_pane.set_var_callbacks(
                var_create=self.gdb.var_create,
                var_list_children=self.gdb.var_list_children,
                var_delete=self.gdb.var_delete,
                var_update=self.gdb.var_update,
                var_eval=self.gdb.eval_expr,
                var_eval_expr=self.gdb.var_evaluate_expression,
            )
        self._locals_pane.set_variables(self._current_locals, self.gdb.current_frame)
        return self._locals_pane


    def _make_register_pane(self: "TGDBApp") -> RegisterPane:
        if self._register_pane is None:
            self._register_pane = RegisterPane(self.hl)
        self._register_pane.set_registers(self._current_registers)
        return self._register_pane


    def _make_stack_pane(self: "TGDBApp") -> StackPane:
        if self.gdb.current_frame:
            current_level = self.gdb.current_frame.level
        else:
            current_level = 0
        if self._stack_pane is None:
            self._stack_pane = StackPane(self.hl)
        self._stack_pane.set_frames(self._current_stack, current_level=current_level)
        return self._stack_pane


    def _make_thread_pane(self: "TGDBApp") -> ThreadPane:
        if self._thread_pane is None:
            self._thread_pane = ThreadPane(self.hl)
        self._thread_pane.set_threads(self._current_threads)
        return self._thread_pane


    def _make_evaluate_pane(self: "TGDBApp") -> EvaluatePane:
        if self._evaluate_pane is None:
            self._evaluate_pane = EvaluatePane(self.hl, self.cfg)
            self._evaluate_pane.set_var_callbacks(
                var_create=self.gdb.var_create,
                var_list_children=self.gdb.var_list_children,
                var_delete=self.gdb.var_delete,
                var_update=self.gdb.var_update,
                var_eval_expr=self.gdb.var_evaluate_expression,
            )
        return self._evaluate_pane


    def _make_memory_pane(self: "TGDBApp") -> MemoryPane:
        """Always return a fresh MemoryPane; multi-instance pane."""
        import weakref
        from .memory_pane import MemoryFormatter
        formatter = self.config._memoryformatter_obj or MemoryFormatter()
        pane = MemoryPane(self.hl, formatter=formatter)
        pane.set_read_fn(self.gdb.read_memory_bytes_async)
        # Subscribe via weak method so pane GC is not blocked.
        try:
            ref = weakref.WeakMethod(pane.set_formatter)

            def _on_formatter_change(obj):
                method = ref()
                if method is None:
                    raise ReferenceError
                method(obj)

            self.config.add_memoryformatter_listener(_on_formatter_change)
        except TypeError:
            # set_formatter not bound (extremely unlikely) — skip subscription.
            pass
        self._memory_panes.append(pane)
        return pane


    def _make_disasm_pane(self: "TGDBApp") -> DisasmPane:
        if self._disasm_pane is None:
            self._disasm_pane = DisasmPane(self.hl)
            self._disasm_pane.set_disasm_fn(self.gdb.request_disassembly_async)
            self._disasm_pane.set_disasm_pc_fn(
                self.gdb.request_disassembly_around_pc_async
            )
            self._disasm_pane.set_disasm_function_fn(
                self.gdb.request_disassembly_function_async
            )
            self._prime_disasm_pane()
        return self._disasm_pane


    def _prime_disasm_pane(self: "TGDBApp") -> None:
        """Fill a freshly-created disasm pane with the current PC or main."""
        pane = self._disasm_pane
        if pane is None:
            return
        frame = self.gdb.current_frame
        if frame is not None and frame.addr:
            supervise(
                pane.refresh_disasm(
                    frame.fullname or frame.file or "",
                    frame.line,
                    current_addr=frame.addr,
                    thread_id=self.gdb.current_thread_id,
                    func=frame.func,
                ),
                name="disasm-pane-prime",
            )
            return
        supervise(pane.prime_function("main"), name="disasm-pane-prime-main")

    # ------------------------------------------------------------------
    # Pane queries
    # ------------------------------------------------------------------

    def _pane_widget(self: "TGDBApp", pane_kind: str) -> Widget | None:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is None:
            return None
        return descriptor.current()


    def _pane_label(self: "TGDBApp", pane_kind: str) -> str:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is not None:
            return descriptor.label
        return pane_kind


    def _pane_is_attached(self: "TGDBApp", pane_kind: str) -> bool:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is not None and descriptor.multi_instance:
            # Multi-instance panes can always be added a new copy from the menu.
            return False
        return self._widget_attached(self._pane_widget(pane_kind))


    def _pane_kind_for_widget(self: "TGDBApp", widget: Widget) -> str | None:
        if isinstance(widget, MemoryPane):
            return "memory"
        for pane_kind, descriptor in self._pane_descriptors.items():
            if descriptor.multi_instance:
                continue
            if widget is descriptor.current():
                return pane_kind
        return None


    def _create_pane(self: "TGDBApp", pane_kind: str) -> Widget | None:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is None:
            return None
        return descriptor.create()

    # ------------------------------------------------------------------
    # Context menu helpers
    # ------------------------------------------------------------------

    def _get_context_menu(self: "TGDBApp") -> ContextMenu | None:
        try:
            return self.query_one("#context-menu", ContextMenu)
        except NoMatches:
            return None


    def _context_menu_contains(self: "TGDBApp", screen_x: int, screen_y: int) -> bool:
        menu = self._get_context_menu()
        if not menu or not menu.is_open:
            return False
        return menu.contains_point(screen_x, screen_y)


    def _build_context_menu_items(self: "TGDBApp") -> list[ContextMenuItem]:
        add_children = []
        for pane_kind in self._add_menu_order:
            if not self._pane_is_attached(pane_kind):
                add_children.append(ContextMenuItem(
                    self._pane_label(pane_kind),
                    action=f"add:{pane_kind}",
                ))
        if not add_children:
            add_children = [ContextMenuItem("No panes available")]

        split_children = [
            ContextMenuItem("⬒ Up", action="split:up"),
            ContextMenuItem("⬓ Down", action="split:down"),
            ContextMenuItem("◧ Left", action="split:left"),
            ContextMenuItem("◨ Right", action="split:right"),
        ]
        pane_items: list[ContextMenuItem] = [
            ContextMenuItem("Add", children=tuple(add_children)),
            ContextMenuItem("Split", children=tuple(split_children)),
            ContextMenuItem("Hide", action="hide", separator_before=True),
            ContextMenuItem("Delete", action="delete"),
        ]

        # Prepend locals-node actions when right-clicking an expandable node.
        node = getattr(self, "_locals_context_node", None)
        if (
            isinstance(self._context_menu_target, LocalVariablePane)
            and node is not None
            and isinstance(getattr(node, "data", None), dict)
            and node.data.get("has_children")
            and not node.data.get("load_more")
        ):
            # Both actions are always available; the distinction (limited vs
            # full load for array/map) is handled inside do_expand_limited.
            locals_items: list[ContextMenuItem] = [
                ContextMenuItem("Expand", children=(
                    ContextMenuItem("Full", action="locals:expand_full"),
                    ContextMenuItem("Limited", action="locals:expand_limited"),
                )),
                ContextMenuItem("Fold", action="locals:fold"),
            ]
            # Visual separator before the standard pane management items.
            pane_items[0] = replace(pane_items[0], separator_before=True)
            return locals_items + pane_items

        return pane_items


    def _open_context_menu(self: "TGDBApp", screen_x: int, screen_y: int) -> None:
        menu = self._get_context_menu()
        if not menu:
            return
        menu.set_items(self._build_context_menu_items())
        menu.open_at(screen_x, screen_y)


    def _restore_focus_after_context_menu(self: "TGDBApp") -> None:
        if self._mode == "FILEDLG":
            try:
                self.query_one("#file-dlg", FileDialog).focus()
                return
            except NoMatches:
                return
        if self._mode == "CMD":
            try:
                self.query_one("#cmdline", CommandLineBar).focus()
                return
            except NoMatches:
                return
        if self._mode in ("GDB_PROMPT", "GDB_SCROLL"):
            if self._focus_widget(self._get_gdb_widget(mounted_only=True)):
                return
        if self._focus_widget(self._get_source_view(mounted_only=True)):
            return
        self._focus_widget(self._first_workspace_leaf())


    def _close_context_menu(self: "TGDBApp", *, restore_focus: bool = True) -> None:
        menu = self._get_context_menu()
        if not menu or not menu.is_open:
            return
        menu.close()
        self._context_menu_target = None
        if restore_focus:
            self._restore_focus_after_context_menu()

    # ------------------------------------------------------------------
    # Workspace tree operations
    # ------------------------------------------------------------------

    def _find_workspace_item(
        self: "TGDBApp", widget: Widget | None
    ) -> Widget | None:
        current = widget
        while isinstance(current, Widget):
            if isinstance(current, Splitter):
                return None
            parent = current.parent
            if isinstance(parent, PaneContainer):
                return current
            if isinstance(parent, Widget):
                current = parent
            else:
                current = None
        return None


    async def _ensure_dynamic_workspace(self: "TGDBApp") -> PaneContainer | None:
        """Return the root PaneContainer (always present since the layout is unified)."""
        try:
            return self.query_one("#split-container", PaneContainer)
        except NoMatches:
            return None


    async def _replace_workspace_item(self: "TGDBApp", target: Widget, new_item: Widget) -> bool:
        if isinstance(target.parent, PaneContainer):
            parent = target.parent
        else:
            parent = None
        if parent is None:
            return False
        await parent.replace_item(target, new_item)
        return True


    async def _normalize_container_after_delete(
        self: "TGDBApp", container: PaneContainer
    ) -> Widget | None:
        current = container
        while True:
            if isinstance(current.parent, PaneContainer):
                parent = current.parent
            else:
                parent = None
            item_count = len(current.items)
            if parent is None:
                if item_count == 0:
                    await current.set_items([EmptyPane(self.hl)])
                return self._first_workspace_leaf()

            if item_count > 1:
                return self._first_workspace_leaf()

            if item_count == 1:
                remaining = await current.take_item(current.items[0])
                await parent.replace_item(current, remaining)
            else:
                await parent.replace_item(current, EmptyPane(self.hl))
            current = parent


    async def _add_pane_to_workspace(self: "TGDBApp", target: Widget, pane_kind: str) -> Widget | None:
        pane = self._create_pane(pane_kind)
        if pane is None:
            return None
        if isinstance(target.parent, PaneContainer):
            parent = target.parent
        else:
            parent = None
        if parent is None:
            return None

        if isinstance(target, EmptyPane):
            await parent.replace_item(target, pane)
        else:
            index = parent.index_of(target) + 1
            await parent.insert_item(index, pane)

        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is not None and descriptor.requester is not None:
            descriptor.requester()
        return pane


    async def _hide_workspace_item(self: "TGDBApp", target: Widget) -> Widget | None:
        if isinstance(target, EmptyPane):
            return target
        replacement = EmptyPane(self.hl)
        if await self._replace_workspace_item(target, replacement):
            return replacement
        return None


    async def _delete_workspace_item(self: "TGDBApp", target: Widget) -> Widget | None:
        if isinstance(target.parent, PaneContainer):
            parent = target.parent
        else:
            parent = None
        if parent is None:
            return None
        await parent.take_item(target)
        return await self._normalize_container_after_delete(parent)


    async def _apply_context_menu_action(self: "TGDBApp", target: Widget, direction: str) -> bool:
        if direction in ("left", "right"):
            axis = "horizontal"
        else:
            axis = "vertical"
        insert_before = direction in ("left", "up")
        if isinstance(target.parent, PaneContainer):
            parent = target.parent
        else:
            parent = None
        if parent is None:
            return False

        if parent.orientation == axis:
            index = parent.index_of(target)
            if not insert_before:
                index += 1
            await parent.insert_item(index, EmptyPane(self.hl))
            return True

        new_container = PaneContainer(self.hl, orientation=axis)
        await parent.replace_item(target, new_container)
        if insert_before:
            await new_container.set_items([EmptyPane(self.hl), target])
        else:
            await new_container.set_items([target, EmptyPane(self.hl)])
        return True

    # ------------------------------------------------------------------
    # Context menu message handlers
    # ------------------------------------------------------------------

    def _get_parent_container(
        self: "TGDBApp", widget: Widget
    ) -> PaneContainer | None:
        """Return *widget*'s parent if it is a PaneContainer, else None."""
        parent = widget.parent
        return parent if isinstance(parent, PaneContainer) else None


    async def _handle_locals_context_action(self: "TGDBApp", target: Widget, sub: str) -> None:
        """Dispatch a 'locals:*' context-menu action on an expandable tree node."""
        node = getattr(self, "_locals_context_node", None)
        self._locals_context_node = None
        if node is not None and isinstance(target, LocalVariablePane):
            if sub == "expand_limited":
                await target.do_expand_limited(node)
            elif sub == "expand_full":
                await target.do_expand_full(node)
            elif sub == "fold":
                target.do_fold(node)


    async def _handle_add_context_action(self: "TGDBApp", target: Widget, pane_kind: str) -> None:
        """Add a pane of *pane_kind* next to *target*."""
        if self._pane_is_attached(pane_kind):
            self._show_status(f"{self._pane_label(pane_kind)} is already shown")
            return
        if await self._add_pane_to_workspace(target, pane_kind) is None:
            self._show_status(f"Unable to add {self._pane_label(pane_kind)}")
            return
        self._show_status(f"Added {self._pane_label(pane_kind)}")


    async def _handle_hide_context_action(self: "TGDBApp", target: Widget) -> None:
        """Replace *target* with an EmptyPane."""
        if isinstance(target, EmptyPane):
            self._show_status("Cell is already empty")
            return
        pane_kind = self._pane_kind_for_widget(target)
        if await self._hide_workspace_item(target) is None:
            self._show_status("Unable to hide cell")
            return
        label = self._pane_label(pane_kind) if pane_kind is not None else "pane"
        self._show_status(f"Hid {label}")


    async def _handle_delete_context_action(self: "TGDBApp", target: Widget) -> None:
        """Remove *target* from the workspace tree."""
        if await self._delete_workspace_item(target) is None:
            self._show_status("Unable to delete cell")
            return
        self._show_status("Deleted cell")


    async def _handle_split_context_action(self: "TGDBApp", target: Widget, direction: str) -> None:
        """Insert a new empty cell adjacent to *target* in *direction*."""
        if await self._apply_context_menu_action(target, direction):
            self._show_status(f"Added window {direction}")
        else:
            self._show_status(f"Unable to add window {direction}")


    async def on_context_menu_selected(self: "TGDBApp", msg: ContextMenuSelected) -> None:
        target = self._context_menu_target
        self._close_context_menu(restore_focus=False)
        if target is None:
            return
        if await self._ensure_dynamic_workspace() is None:
            self._show_status("Unable to create workspace container")
            self._restore_focus_after_context_menu()
            return

        action = msg.action
        try:
            if action.startswith("locals:"):
                await self._handle_locals_context_action(
                    target, action[len("locals:"):]
                )
            elif action.startswith("add:"):
                await self._handle_add_context_action(
                    target, action.split(":", 1)[1]
                )
            elif action == "hide":
                await self._handle_hide_context_action(target)
            elif action == "delete":
                await self._handle_delete_context_action(target)
            elif action.startswith("split:"):
                await self._handle_split_context_action(
                    target, action.split(":", 1)[1]
                )
            else:
                self._show_status(f"Context menu: {action}")
        finally:
            self._restore_focus_after_context_menu()


    def on_context_menu_closed(self: "TGDBApp", _: ContextMenuClosed) -> None:
        self._close_context_menu()
