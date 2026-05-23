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

import asyncio
import enum
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import TGDBApp


_log = logging.getLogger("tgdb.api")


# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------


class SplitMode(enum.Enum):
    """Orientation of a split operation."""

    HORIZONTAL = "horizontal"  # left / right  (vertical divider line)
    VERTICAL = "vertical"  # top / bottom  (horizontal divider line)


class Pane(enum.Enum):
    """Named pane types that can be attached to workspace cells."""

    SOURCE = "source"
    GDB = "gdb"
    LOCALS = "locals"
    REGISTERS = "registers"
    STACK = "stack"
    THREADS = "threads"
    EVALUATE = "evaluate"
    MEMORY = "memory"
    DISASM = "disasm"


def _pane_label(p) -> str:
    """Stable string form of a Pane / SplitMode value for log output."""
    if isinstance(p, (Pane, SplitMode)):
        return p.value
    return str(p)


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
        _log.info(f"api: attach(address={self._address}, pane={_pane_label(pane_type)})")
        await self._screen._do_attach(self._address, pane_type)


    async def detach(self) -> None:
        """Replace the cell's content with an EmptyPane."""
        _log.info(f"api: detach(address={self._address})")
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
        self._app: "TGDBApp" | None = None


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
        _log.info("api: close_all_panes()")
        await self._do_close_all()


    async def split(self, pane: list[int] | None = None, mode: SplitMode = SplitMode.HORIZONTAL) -> None:
        """Add a new empty cell relative to the cell at *pane*.

        If *pane* is ``[]`` (or omitted), the new cell is added to the root
        container (ensuring the root has the given *mode* orientation).

        Otherwise, the target cell is found by *pane* address:

        * If the target's parent container has the **same** orientation as
          *mode*, a new sibling cell is inserted immediately after the target.
        * If the orientations differ, the target is wrapped in a new container
          of the requested *mode* and the new empty cell is added alongside it.
        """
        target = [] if pane is None else list(pane)
        _log.info(f"api: split(pane={target}, mode={_pane_label(mode)})")
        await self._do_split(target, mode)


    async def close(self, address: list[int]) -> None:
        """Delete the workspace cell at *address* (equivalent to context-menu Delete)."""
        _log.info(f"api: close(address={list(address)})")
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
                    f"Path element [{step}]={i}: expected PaneContainer, got {type(current).__name__}"
                )
            items = current.items
            if i < 0 or i >= len(items):
                raise IndexError(
                    f"Path element [{step}]={i}: index out of range (container has {len(items)} items)"
                )
            current = items[i]
        return current


    async def _do_close_all(self) -> None:
        from .workspace import EmptyPane

        app = self._app
        if app is None:
            return
        root = await app._ensure_dynamic_workspace()
        if root is None:
            return
        await root.set_items([EmptyPane(app.hl)])


    async def _do_split(self, pane: list[int], mode: SplitMode) -> None:
        from .workspace import EmptyPane, PaneContainer

        app = self._app
        if app is None:
            return
        await app._ensure_dynamic_workspace()
        if isinstance(mode, SplitMode):
            orientation = mode.value
        else:
            orientation = str(mode)
        if orientation == "horizontal":
            direction = "right"
        else:
            direction = "down"

        if not pane:
            # Operate on the root container: ensure orientation, add cell.
            # ``set_orientation_async`` (rather than the sync variant) so
            # the rebuild completes inline; otherwise the sync variant's
            # ``call_later(_rebuild)`` fires during ``insert_item``'s
            # ``await`` and detaches widgets mid-mount.
            root = app.query_one("#split-container", PaneContainer)
            if root.orientation != orientation:
                await root.set_orientation_async(orientation)
            await root.insert_item(len(root.items), EmptyPane(app.hl))
        else:
            widget = self._get_widget_at(pane)
            if isinstance(widget, PaneContainer):
                # Directly address a sub-container: add a child to it.
                # Same async-orientation rationale as the root branch.
                if widget.orientation != orientation:
                    await widget.set_orientation_async(orientation)
                await widget.insert_item(len(widget.items), EmptyPane(app.hl))
            else:
                await app._apply_context_menu_action(widget, direction)


    async def _do_close(self, address: list[int]) -> None:
        app = self._app
        if app is None:
            return
        widget = self._get_widget_at(address)
        await app._delete_workspace_item(widget)


    async def _do_attach(self, address: list[int], pane_type: Pane) -> None:
        from .workspace import EmptyPane, PaneContainer

        app = self._app
        if app is None:
            return
        if isinstance(pane_type, Pane):
            pane_kind = pane_type.value
        else:
            pane_kind = str(pane_type)
        widget = self._get_widget_at(address)
        pane_widget = app._create_pane(pane_kind)
        if pane_widget is None:
            return

        # Non-multi-instance descriptors (source, gdb, locals, registers,
        # stack, threads, evaluate) return a SINGLETON widget — the same
        # Python instance regardless of how many times the user re-attaches.
        # Textual's ``Widget.mount`` short-circuits to a no-op when the
        # widget passed is already in ``app._registry`` (i.e. attached to
        # any parent in the DOM, including a stale parent left over from a
        # previous attach).  That short-circuit caused the layout breakage
        # reported when the user ran:
        #
        #     await tgdb.screen.close_all_panes()
        #     await tgdb.screen.split([], mode=HORIZONTAL)
        #     await tgdb.screen.split([0], mode=VERTICAL)
        #     await tgdb.screen.get_pane([0,0]).attach(Pane.SOURCE)
        #     await tgdb.screen.get_pane([0,1]).attach(Pane.GDB)   # <- broke
        #
        # The GDB singleton was still registered against its prior parent
        # (the pre-close_all root) when ``replace_item`` issued
        # ``mount(gdb_widget, after=empty_C)``; the mount silently
        # no-op'd because the registry hit, then ``empty_C.remove()``
        # detached the placeholder, leaving the vertical sub-container
        # with no DOM children and the GDB widget rendering at its old
        # location (or freshly orphaned during the pending prune).
        #
        # Detach the singleton from any prior location BEFORE handing it
        # to ``replace_item`` so the upcoming mount actually attaches it
        # to the new parent.  Two cases:
        #
        #   1. Singleton is mounted under another ``PaneContainer``:
        #      replace it there with a fresh EmptyPane so the user's old
        #      slot does not collapse.
        #   2. Singleton is attached to something else (an intermediate
        #      detach-but-not-unregister state, or a non-PaneContainer
        #      parent): call ``remove()`` directly.  The ``AwaitRemove``
        #      is awaited to guarantee the registry entry is gone before
        #      the new mount runs.
        #
        # If the singleton's destination slot is itself (e.g. attaching
        # it back to where it already lives), there is no work to do.
        if pane_widget is widget:
            return
        if pane_widget.is_mounted:
            old_parent = pane_widget.parent
            if isinstance(old_parent, PaneContainer):
                await old_parent.replace_item(pane_widget, EmptyPane(app.hl))
            elif old_parent is not None:
                await pane_widget.remove()

        parent = widget.parent
        if not isinstance(parent, PaneContainer):
            # Target slot is not under a PaneContainer — nothing to
            # replace, and triggering ``requester()`` for an unattached
            # pane would refresh data the user can never see.
            return
        await parent.replace_item(widget, pane_widget)
        descriptor = app._pane_descriptors.get(pane_kind)
        if descriptor is not None and descriptor.requester is not None:
            result = descriptor.requester()
            if asyncio.iscoroutine(result):
                await result


    async def _do_detach(self, address: list[int]) -> None:
        app = self._app
        if app is None:
            return
        widget = self._get_widget_at(address)
        await app._hide_workspace_item(widget)


# ---------------------------------------------------------------------------
# Module-level singleton (the ``tgdb.screen`` object)
# ---------------------------------------------------------------------------

screen = TGDBScreen()
