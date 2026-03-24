"""
tgdb standard-library namespace — exposes workspace, screen, and pane
manipulation to :python scripts as ``import tgdb; await tgdb.screen.split(...)``.

Design notes
------------
All write operations (split, close, attach, detach, close_all_panes) are
``async def`` coroutines.  Because :python heredocs now run as a proper
``async def _tgdb_script()`` coroutine (awaited from the command task),
scripts can simply ``await`` these operations directly:

    await tgdb.screen.close_all_panes()
    await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.HORIZONTAL)
    src = tgdb.screen.get_pane([0])
    await src.attach(tgdb.Pane.SOURCE)

Each await suspends the script, lets Textual complete the DOM mutation
(mount/remove etc.), then resumes the script.  This guarantees that
earlier splits are fully applied before later address lookups run.

- Read operations (size, width, height, get_pane) remain synchronous.
- ``get_pane(address)`` returns a PaneHandle immediately; the address is
  resolved against the live widget tree when the async attach/detach runs.


Pane addresses
--------------
A pane address is a list of integer indices that navigate the workspace
container tree starting from the root PaneContainer:

  []          → root container itself (used only for split())
  [0]         → first child of root
  [1]         → second child of root
  [1, 0]      → first child of the second child of root (which must be a container)
  [1, 1, 2]   → second child of first child of second child of root
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .app import TGDBApp


# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------

class SplitMode(enum.Enum):
    """Orientation of a split operation."""
    HORIZONTAL = "horizontal"   # left / right  (vertical divider line)
    VERTICAL = "vertical"       # top / bottom  (horizontal divider line)


class Pane(enum.Enum):
    """Named pane types that can be attached to workspace cells."""
    SOURCE = "source"
    GDB = "gdb"
    LOCALS = "locals"
    REGISTERS = "registers"
    STACK = "stack"
    THREADS = "threads"


# ---------------------------------------------------------------------------
# PaneHandle
# ---------------------------------------------------------------------------

class PaneHandle:
    """A lazy reference to a workspace cell identified by its address.

    The address is resolved against the *live* widget tree when the async
    operation actually fires, so it is safe to create a PaneHandle before
    the splits that produce its cell have been applied.
    """

    def __init__(self, screen: "TGDBScreen", address: list[int]) -> None:
        self._screen = screen
        self._address = address

    async def attach(self, pane_type: Pane) -> None:
        """Replace the cell's current content with a named pane widget."""
        await self._screen._do_attach(self._address, pane_type)

    async def detach(self) -> None:
        """Replace the cell's content with an EmptyPane."""
        await self._screen._do_detach(self._address)

    def __repr__(self) -> str:
        return f"PaneHandle({self._address})"


# ---------------------------------------------------------------------------
# TGDBScreen
# ---------------------------------------------------------------------------

class TGDBScreen:
    """
    ``tgdb.screen`` — workspace and screen dimension API.

    Usage example::

        import tgdb
        tgdb.screen.close_all_panes()
        tgdb.screen.split(pane=[], mode=tgdb.SplitMode.HORIZONTAL)
        src = tgdb.screen.get_pane([0])
        src.attach(tgdb.Pane.SOURCE)
    """

    def __init__(self) -> None:
        self._app: Optional["TGDBApp"] = None

    def _set_app(self, app: "TGDBApp") -> None:
        self._app = app

    # ------------------------------------------------------------------
    # Read-only queries (synchronous)
    # ------------------------------------------------------------------

    def size(self) -> tuple[int, int]:
        """Return ``(width, height)`` of the terminal in cells."""
        if self._app is None:
            return (80, 24)
        s = self._app.size
        return (s.width, s.height)

    def width(self) -> int:
        """Return terminal width in cells."""
        return self.size()[0]

    def height(self) -> int:
        """Return terminal height in cells."""
        return self.size()[1]

    def get_pane(self, address: list[int]) -> PaneHandle:
        """Return a lazy handle for the workspace cell at *address*."""
        return PaneHandle(self, list(address))

    # ------------------------------------------------------------------
    # Write operations — async coroutines; call with ``await``
    # ------------------------------------------------------------------

    async def close_all_panes(self) -> None:
        """Remove all workspace panes and reset to a single empty cell."""
        await self._do_close_all()

    async def split(self, pane: list[int] | None = None,
                    mode: SplitMode = SplitMode.HORIZONTAL) -> None:
        """Add a new empty cell relative to the cell at *pane*.

        If *pane* is ``[]`` (or omitted), the new cell is added to the root
        container (ensuring the root has the given *mode* orientation).

        Otherwise, the target cell is found by *pane* address:

        * If the target's parent container has the **same** orientation as
          *mode*, a new sibling cell is inserted immediately after the target.
        * If the orientations differ, the target is wrapped in a new container
          of the requested *mode* and the new empty cell is added alongside it.
        """
        await self._do_split(list(pane) if pane is not None else [], mode)

    async def close(self, address: list[int]) -> None:
        """Delete the workspace cell at *address* (equivalent to context-menu Delete)."""
        await self._do_close(list(address))

    # ------------------------------------------------------------------
    # Async implementation helpers
    # ------------------------------------------------------------------

    def _get_widget_at(self, address: list[int]):
        """Synchronously navigate the live widget tree to the node at *address*."""
        from .workspace import PaneContainer
        try:
            root = self._app.query_one("#split-container", PaneContainer)
        except Exception as exc:
            raise ValueError("Workspace not ready (no #split-container)") from exc

        current = root
        for step, i in enumerate(address):
            if not isinstance(current, PaneContainer):
                raise ValueError(
                    f"Path element [{step}]={i}: expected PaneContainer, "
                    f"got {type(current).__name__}"
                )
            items = current.items
            if i < 0 or i >= len(items):
                raise IndexError(
                    f"Path element [{step}]={i}: index out of range "
                    f"(container has {len(items)} items)"
                )
            current = items[i]
        return current

    async def _do_close_all(self) -> None:
        from .workspace import EmptyPane, PaneContainer
        app = self._app
        if app is None:
            return
        root = await app._ensure_dynamic_workspace()
        if root is None:
            return
        for item in list(root.items):
            await root.take_item(item)
        await root.insert_item(0, EmptyPane())

    async def _do_split(self, pane: list[int], mode: SplitMode) -> None:
        from .workspace import EmptyPane, PaneContainer
        app = self._app
        if app is None:
            return
        await app._ensure_dynamic_workspace()
        orientation = mode.value if isinstance(mode, SplitMode) else str(mode)
        direction = "right" if orientation == "horizontal" else "down"

        if not pane:
            # Operate on the root container: ensure orientation, add cell
            root = app.query_one("#split-container", PaneContainer)
            if root.orientation != orientation:
                root.set_orientation(orientation)
            await root.insert_item(len(root.items), EmptyPane())
        else:
            widget = self._get_widget_at(pane)
            if isinstance(widget, PaneContainer):
                # Directly address a sub-container: add a child to it
                if widget.orientation != orientation:
                    widget.set_orientation(orientation)
                await widget.insert_item(len(widget.items), EmptyPane())
            else:
                await app._apply_context_menu_action(widget, direction)

    async def _do_close(self, address: list[int]) -> None:
        app = self._app
        if app is None:
            return
        widget = self._get_widget_at(address)
        await app._delete_workspace_item(widget)

    async def _do_attach(self, address: list[int], pane_type: Pane) -> None:
        from .workspace import PaneContainer
        app = self._app
        if app is None:
            return
        pane_kind = pane_type.value if isinstance(pane_type, Pane) else str(pane_type)
        widget = self._get_widget_at(address)
        pane_widget = app._create_pane(pane_kind)
        if pane_widget is None:
            return
        parent = widget.parent
        if isinstance(parent, PaneContainer):
            await parent.replace_item(widget, pane_widget)
        descriptor = app._pane_descriptors.get(pane_kind)
        if descriptor is not None and descriptor.requester is not None:
            descriptor.requester()

    async def _do_detach(self, address: list[int]) -> None:
        from .workspace import EmptyPane, PaneContainer
        app = self._app
        if app is None:
            return
        widget = self._get_widget_at(address)
        await app._hide_workspace_item(widget)


# ---------------------------------------------------------------------------
# Module-level singleton (the ``tgdb.screen`` object)
# ---------------------------------------------------------------------------

screen = TGDBScreen()
