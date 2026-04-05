"""WorkspaceMixin — dynamic workspace / pane management extracted from TGDBApp."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

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
from .workspace import EmptyPane, PaneContainer, Splitter

if TYPE_CHECKING:
    from .app import TGDBApp


class WorkspaceMixin:
    """Dynamic workspace / pane management."""

    # ------------------------------------------------------------------
    # Pane factories
    # ------------------------------------------------------------------

    def _make_source_pane(self: TGDBApp) -> SourceView:
        if self._source_view is None:
            self._source_view = SourceView(self.hl, id="src-pane")
        return self._source_view

    def _make_gdb_pane(self: TGDBApp) -> GDBWidget:
        if self._gdb_widget is None:
            self._gdb_widget = GDBWidget(
                self.hl,
                max_scrollback=self.cfg.scrollbackbuffersize,
                id="gdb-pane",
            )
        return self._gdb_widget

    def _make_local_variable_pane(self: TGDBApp) -> LocalVariablePane:
        if self._locals_pane is None:
            self._locals_pane = LocalVariablePane(self.hl)
            self._locals_pane.set_var_callbacks(
                var_create=self.gdb.var_create,
                var_list_children=self.gdb.var_list_children,
                var_delete=self.gdb.var_delete,
                var_update=self.gdb.var_update,
            )
        self._locals_pane.set_variables(self._current_locals)
        return self._locals_pane

    def _make_register_pane(self: TGDBApp) -> RegisterPane:
        if self._register_pane is None:
            self._register_pane = RegisterPane(self.hl)
        self._register_pane.set_registers(self._current_registers)
        return self._register_pane

    def _make_stack_pane(self: TGDBApp) -> StackPane:
        current_level = self.gdb.current_frame.level if self.gdb.current_frame else 0
        if self._stack_pane is None:
            self._stack_pane = StackPane(self.hl)
        self._stack_pane.set_frames(self._current_stack, current_level=current_level)
        return self._stack_pane

    def _make_thread_pane(self: TGDBApp) -> ThreadPane:
        if self._thread_pane is None:
            self._thread_pane = ThreadPane(self.hl)
        self._thread_pane.set_threads(self._current_threads)
        return self._thread_pane

    # ------------------------------------------------------------------
    # Pane queries
    # ------------------------------------------------------------------

    def _pane_widget(self: TGDBApp, pane_kind: str) -> Optional[Widget]:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is None:
            return None
        return descriptor.current()

    def _pane_label(self: TGDBApp, pane_kind: str) -> str:
        descriptor = self._pane_descriptors.get(pane_kind)
        return descriptor.label if descriptor is not None else pane_kind

    def _pane_is_attached(self: TGDBApp, pane_kind: str) -> bool:
        return self._widget_attached(self._pane_widget(pane_kind))

    def _pane_kind_for_widget(self: TGDBApp, widget: Widget) -> Optional[str]:
        for pane_kind, descriptor in self._pane_descriptors.items():
            if widget is descriptor.current():
                return pane_kind
        return None

    def _create_pane(self: TGDBApp, pane_kind: str) -> Optional[Widget]:
        descriptor = self._pane_descriptors.get(pane_kind)
        if descriptor is None:
            return None
        return descriptor.create()

    # ------------------------------------------------------------------
    # Context menu helpers
    # ------------------------------------------------------------------

    def _get_context_menu(self: TGDBApp) -> Optional[ContextMenu]:
        try:
            return self.query_one("#context-menu", ContextMenu)
        except NoMatches:
            return None

    def _context_menu_contains(self: TGDBApp, screen_x: int, screen_y: int) -> bool:
        menu = self._get_context_menu()
        if not menu or not menu.is_open:
            return False
        return menu.contains_point(screen_x, screen_y)

    def _build_context_menu_items(self: TGDBApp) -> list[ContextMenuItem]:
        add_children = [
            ContextMenuItem(
                self._pane_label(pane_kind),
                action=f"add:{pane_kind}",
            )
            for pane_kind in self._add_menu_order
            if not self._pane_is_attached(pane_kind)
        ]
        if not add_children:
            add_children = [ContextMenuItem("No panes available")]

        split_children = [
            ContextMenuItem("⬒ Up", action="split:up"),
            ContextMenuItem("⬓ Down", action="split:down"),
            ContextMenuItem("◧ Left", action="split:left"),
            ContextMenuItem("◨ Right", action="split:right"),
        ]
        return [
            ContextMenuItem("Add", children=tuple(add_children)),
            ContextMenuItem("Split", children=tuple(split_children)),
            ContextMenuItem("Hide", action="hide", separator_before=True),
            ContextMenuItem("Delete", action="delete"),
        ]

    def _open_context_menu(self: TGDBApp, screen_x: int, screen_y: int) -> None:
        menu = self._get_context_menu()
        if not menu:
            return
        menu.set_items(self._build_context_menu_items())
        menu.open_at(screen_x, screen_y)

    def _restore_focus_after_context_menu(self: TGDBApp) -> None:
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

    def _close_context_menu(self: TGDBApp, *, restore_focus: bool = True) -> None:
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

    def _find_workspace_item(self: TGDBApp, widget: Optional[Widget]) -> Optional[Widget]:
        current = widget
        while isinstance(current, Widget):
            if isinstance(current, Splitter):
                return None
            parent = current.parent
            if isinstance(parent, PaneContainer):
                return current
            current = parent if isinstance(parent, Widget) else None
        return None

    async def _ensure_dynamic_workspace(self: TGDBApp) -> Optional[PaneContainer]:
        """Return the root PaneContainer (always present since the layout is unified)."""
        try:
            return self.query_one("#split-container", PaneContainer)
        except NoMatches:
            return None

    async def _replace_workspace_item(self: TGDBApp, target: Widget, new_item: Widget) -> bool:
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
        if parent is None:
            return False
        await parent.replace_item(target, new_item)
        return True

    async def _normalize_container_after_delete(self: TGDBApp, container: PaneContainer) -> Optional[Widget]:
        current = container
        while True:
            parent = current.parent if isinstance(current.parent, PaneContainer) else None
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

    async def _add_pane_to_workspace(self: TGDBApp, target: Widget, pane_kind: str) -> Optional[Widget]:
        pane = self._create_pane(pane_kind)
        if pane is None:
            return None
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
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

    async def _hide_workspace_item(self: TGDBApp, target: Widget) -> Optional[Widget]:
        if isinstance(target, EmptyPane):
            return target
        replacement = EmptyPane(self.hl)
        if await self._replace_workspace_item(target, replacement):
            return replacement
        return None

    async def _delete_workspace_item(self: TGDBApp, target: Widget) -> Optional[Widget]:
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
        if parent is None:
            return None
        await parent.take_item(target)
        return await self._normalize_container_after_delete(parent)

    async def _apply_context_menu_action(self: TGDBApp, target: Widget, direction: str) -> bool:
        axis = "horizontal" if direction in ("left", "right") else "vertical"
        insert_before = direction in ("left", "up")
        parent = target.parent if isinstance(target.parent, PaneContainer) else None
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

    async def on_context_menu_selected(self: TGDBApp, msg: ContextMenuSelected) -> None:
        target = self._context_menu_target
        self._close_context_menu(restore_focus=False)
        if target is None:
            return
        if await self._ensure_dynamic_workspace() is None:
            self._show_status("Unable to create workspace container")
            self._restore_focus_after_context_menu()
            return

        action = msg.action

        if action.startswith("add:"):
            pane_kind = action.split(":", 1)[1]
            if self._pane_is_attached(pane_kind):
                self._show_status(f"{self._pane_label(pane_kind)} is already shown")
                self._restore_focus_after_context_menu()
                return
            if await self._add_pane_to_workspace(target, pane_kind) is None:
                self._show_status(f"Unable to add {self._pane_label(pane_kind)}")
                self._restore_focus_after_context_menu()
                return
            self._show_status(f"Added {self._pane_label(pane_kind)}")
            self._restore_focus_after_context_menu()
            return

        if action == "hide":
            pane_kind = self._pane_kind_for_widget(target)
            if isinstance(target, EmptyPane):
                self._show_status("Cell is already empty")
                self._restore_focus_after_context_menu()
                return
            if await self._hide_workspace_item(target) is None:
                self._show_status("Unable to hide cell")
                self._restore_focus_after_context_menu()
                return
            label = self._pane_label(pane_kind) if pane_kind is not None else "pane"
            self._show_status(f"Hid {label}")
            self._restore_focus_after_context_menu()
            return

        if action == "delete":
            if await self._delete_workspace_item(target) is None:
                self._show_status("Unable to delete cell")
                self._restore_focus_after_context_menu()
                return
            self._show_status("Deleted cell")
            self._restore_focus_after_context_menu()
            return

        direction = action.split(":", 1)[1] if action.startswith("split:") else None
        if direction is None:
            self._show_status(f"Context menu: {action}")
            self._restore_focus_after_context_menu()
            return

        if await self._apply_context_menu_action(target, direction):
            self._show_status(f"Added window {direction}")
        else:
            self._show_status(f"Unable to add window {direction}")
        self._restore_focus_after_context_menu()

    def on_context_menu_closed(self: TGDBApp, _: ContextMenuClosed) -> None:
        self._close_context_menu()
