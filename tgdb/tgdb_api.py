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
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import TGDBApp
    from .gdb_controller.types import LocalVariable


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
        old_items = root.items
        await root.set_items([EmptyPane(app.hl)])
        for item in old_items:
            app._dispose_workspace_item(item)


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
        app._dispose_workspace_item(widget)
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
# TGDBDebugger
# ---------------------------------------------------------------------------

# CSI / OSC escape sequences and readline's prompt markers, stripped before
# looking for the GDB prompt in console output.
_TERMINAL_ESCAPES_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|[\x01\x02\r]")


class TGDBDebugger:
    """
    ``tgdb.debugger`` — drive GDB and read debugger state from scripts.

    Intended for ``:python`` blocks, ``--input`` / ``--batch`` scripts, and
    tests.  The coroutines wait on the App's message loop, so await them
    from a script (which runs in its own task), never from inside a Textual
    message handler.

    Usage example::

        import tgdb
        dbg = tgdb.debugger
        await dbg.execute("break main")
        await dbg.execute("run")            # returns once stopped + panes updated
        print(dbg.source_position())        # ('/path/main.c', 12)
        print(await dbg.evaluate("argc"))   # '1'
        print(dbg.locals())                 # [('argc', '1'), ('argv', '0x7ffe...')]
        await dbg.command("evaluate argc * 2")
        print(dbg.watches())                # [('argc * 2', '2')]

    Public methods:

    - ``await execute(command)`` — type a GDB command into the console as
      the user would, then wait until GDB shows its prompt again and the
      panes have caught up.
    - ``await wait_idle()`` — wait until the inferior is stopped, no MI
      request is in flight, and queued UI updates have been applied.
    - ``await command(line)`` — run a tgdb ``:`` command; returns its
      message (usually an error) or ``None``.
    - ``await evaluate(expr)`` — value of *expr* in the selected frame,
      formatted like the Locals / Evaluations panes; raises
      ``RuntimeError`` when GDB cannot evaluate it.
    - ``source_position()`` — ``(path, executing_line)`` of the source pane.
    - ``local_variables()`` — locals of the selected frame as collected
      from GDB (``LocalVariable`` records with address and shadowing).
    - ``locals()`` / ``watches()`` — top-level ``(expression, value)`` rows
      shown in the Locals / Evaluations pane (``[]`` when not open).

    Timeouts raise ``TimeoutError``; ``execute`` includes the tail of the
    GDB console in the message.
    """

    def __init__(self) -> None:
        self._app: "TGDBApp" | None = None


    def _set_app(self, app: "TGDBApp") -> None:
        self._app = app


    def _require_app(self) -> "TGDBApp":
        if self._app is None:
            raise RuntimeError("tgdb.debugger is not attached to a running tgdb")
        return self._app



    # ------------------------------------------------------------------
    # Driving GDB (async coroutines; call with ``await``)
    # ------------------------------------------------------------------


    async def execute(self, command: str, timeout: float = 30.0) -> None:
        """Type *command* into the GDB console and wait for it to finish.

        "Finished" means GDB printed its prompt again (for ``run``,
        ``next``, ``continue`` … that is after the inferior stopped) and
        :meth:`wait_idle` returned.  A command that leaves the inferior
        running or waits for an answer (``y or n``) times out.
        """
        app = self._require_app()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        prompt = _TERMINAL_ESCAPES_RE.sub("", await self._prompt(timeout))
        output = bytearray()
        prompt_seen = asyncio.Event()

        def on_console(data: bytes) -> None:
            output.extend(data)
            text = _TERMINAL_ESCAPES_RE.sub("", output.decode("utf-8", errors="replace"))
            if f"\n{prompt}" in text:
                prompt_seen.set()

        gdb = app.gdb
        gdb.add_console_listener(on_console)
        try:
            _log.info(f"api: execute({command!r})")
            gdb.send_input((command + "\r").encode())
            try:
                await asyncio.wait_for(prompt_seen.wait(), timeout)
            except TimeoutError:
                tail = output.decode("utf-8", errors="replace")[-2000:]
                raise TimeoutError(
                    f"GDB did not finish {command!r} within {timeout}s; console output:\n{tail}"
                ) from None
        finally:
            gdb.remove_console_listener(on_console)
        await self.wait_idle(max(1.0, deadline - loop.time()))


    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Wait until GDB and the panes are quiet.

        Repeats an MI round trip (so every earlier MI record has been
        parsed) and a UI round trip (so every update queued from those
        records has run) until two rounds in a row find no running
        inferior, no in-flight MI request, and no locals reconciliation.
        """
        app = self._require_app()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        quiet_rounds = 0
        while quiet_rounds < 2:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"tgdb did not become idle within {timeout}s")
            await self._mi_round_trip(remaining)
            await self._ui_round_trip(max(0.1, deadline - loop.time()))
            if self._busy(app):
                quiet_rounds = 0
            else:
                quiet_rounds += 1
            await asyncio.sleep(0.05)


    async def command(self, line: str) -> str | None:
        """Run a tgdb ``:`` command (e.g. ``"evaluate x"``, ``"set ts=4"``)."""
        app = self._require_app()
        _log.info(f"api: command({line!r})")
        return await app.cp.execute_async(line)


    async def evaluate(self, expr: str) -> str:
        """Value of *expr* in the selected frame, formatted as the panes show it."""
        gdb = self._require_app().gdb
        info = await gdb.var_create(expr)
        name = info.get("name", "")
        try:
            return str(info.get("value", ""))
        finally:
            if name:
                await gdb.var_delete(name)



    # ------------------------------------------------------------------
    # Reading state (synchronous)
    # ------------------------------------------------------------------


    def source_position(self) -> tuple[str, int] | None:
        """``(path, executing_line)`` of the file shown in the source pane.

        *executing_line* is 0 when the executing line is not in that file
        (e.g. before ``run``).  ``None`` when no file is shown.
        """
        src = self._require_app()._get_source_view()
        if src is None or src.source_file is None:
            return None
        return (src.source_file.path, src.exe_line)


    def local_variables(self) -> list["LocalVariable"]:
        """Locals of the selected frame as last collected from GDB."""
        return list(self._require_app()._current_locals)


    def locals(self) -> list[tuple[str, str]]:
        """``(name, value)`` rows of the Locals pane; ``[]`` if never opened."""
        pane = self._require_app()._locals_pane
        if pane is None:
            return []
        return pane.rows()


    def watches(self) -> list[tuple[str, str]]:
        """``(expression, value)`` rows of the Evaluations pane; ``[]`` if never opened."""
        pane = self._require_app()._evaluate_pane
        if pane is None:
            return []
        return pane.rows()



    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


    async def _prompt(self, timeout: float) -> str:
        """The current GDB prompt string (``set prompt`` aware)."""
        result = await self._require_app().gdb.mi_command_async("-gdb-show prompt", timeout=timeout)
        payload = result.get("payload") or {}
        value = payload.get("value") if isinstance(payload, dict) else None
        if isinstance(value, str) and value.strip():
            return value
        return "(gdb) "


    async def _mi_round_trip(self, timeout: float) -> None:
        gdb = self._require_app().gdb
        result = await gdb.mi_command_async("-gdb-show prompt", timeout=timeout)
        if not result:
            # Transport not ready yet (startup) or timed out; let the
            # caller's deadline decide.
            await asyncio.sleep(0.1)


    async def _ui_round_trip(self, timeout: float) -> None:
        app = self._require_app()
        done = asyncio.get_running_loop().create_future()

        def mark_done() -> None:
            if not done.done():
                done.set_result(None)

        app.call_later(mark_done)
        await asyncio.wait_for(done, timeout)


    @staticmethod
    def _busy(app: "TGDBApp") -> bool:
        if app.gdb is None or app.gdb.is_busy():
            return True
        pane = app._locals_pane
        return pane is not None and pane._reconcile_active > 0


# ---------------------------------------------------------------------------
# Module-level singletons (the ``tgdb.screen`` / ``tgdb.debugger`` objects)
# ---------------------------------------------------------------------------

screen = TGDBScreen()
debugger = TGDBDebugger()
