"""
Public widget entry point for the local-variable-pane package.

``LocalVariablePane`` is designed to be used as a black-box Textual widget.
Its caller is responsible for two things only:

1. Construct the widget with the highlight/config objects it needs.
2. Inject the async debugger callbacks and push new local-variable snapshots
   through the public methods documented on the class below.

Once those dependencies are set, the pane owns the rest of the behavior:
varobj creation/deletion, lazy child loading, expansion-state persistence,
shadowed-variable tracking, and reconciliation between successive debugger
stops.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable, Coroutine, Optional

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ..config_types import Config
from ..gdb_types import Frame, LocalVariable
from ..highlight_groups import HighlightGroups
from ..pane_base import PaneBase
from .reconcile import LocalVariablePaneReconcileMixin
from .support import LocalVariablePaneSupportMixin
from .tree import LocalVariablePaneTreeMixin
from .update import LocalVariablePaneUpdateMixin


class LocalVariablePane(
    LocalVariablePaneReconcileMixin,
    LocalVariablePaneUpdateMixin,
    LocalVariablePaneTreeMixin,
    LocalVariablePaneSupportMixin,
    PaneBase,
):
    """Render the current frame's local variables as an expandable tree.

    Public interface
    ----------------
    ``LocalVariablePane`` intentionally exposes a small caller-facing API:

    ``LocalVariablePane(hl, cfg, **kwargs)``
        Create the widget. At construction time the pane has no debugger I/O
        attached yet; it is just a UI object with empty internal state.

    ``set_var_callbacks(...)``
        Inject the async callbacks that talk to GDB/MI. This is the dependency
        injection point that makes the pane self-contained and reusable.

    ``set_variables(variables, frame)``
        Publish the latest locals snapshot for the current frame. This is the
        main state-mutation API. Call it whenever the inferior stops or when
        the running/stopped state changes.

    ``do_expand_some(node)``, ``do_expand_all(node)``, ``do_fold(node)``
        Optional subtree actions used by the context-menu layer. They are part
        of the public widget contract because other UI code may call them, but
        they do not need any direct knowledge of the internals beyond passing a
        ``TreeNode`` that belongs to this pane.

    Callback contract
    -----------------
    ``set_var_callbacks`` expects callbacks compatible with the methods exposed
    by ``GDBController`` / ``VarobjMixin``:

    ``var_create(expr) -> dict``
        Must create a GDB varobj and return a payload containing fields such as
        ``name``, ``value``, ``numchild``, ``dynamic``, ``type``, and
        ``displayhint``.

    ``var_list_children(varobj_name, from_idx=0, limit=0) -> (children, has_more)``
        Must return a list of MI child payloads plus a boolean telling the pane
        whether a "load more" sentinel should be shown.

    ``var_delete(varobj_name) -> None``
        Must delete a varobj and its descendants on the debugger side.

    ``var_update(varobj_name="*", timeout=10.0) -> list[dict]``
        Must perform ``-var-update`` and return the MI changelist payload.

    ``var_eval(expr) -> str``
        Must evaluate a raw GDB expression such as ``&name``.

    ``var_eval_expr(varobj_name) -> str``
        Must evaluate the current value string of an existing varobj without
        re-enumerating its children.

    ``get_decl_lines() -> dict[str, int]``
        Must return declaration line information for locals in the current
        frame. The pane uses this to hide variables whose constructors have not
        run yet.

    Behavior contract
    -----------------
    After the callbacks are installed, the pane behaves as follows:

    - The widget tracks variables by ``(name, stack_address)`` so shadowed
      bindings remain distinct.
    - Expansion state is preserved per frame signature and restored when the
      user revisits the same frame shape.
    - Child nodes are loaded lazily from GDB only when needed.
    - Array/map-like pretty-printer nodes obey ``cfg.expandchildlimit`` for
      "expand some" behavior; other compound types expand fully.
    - ``set_variables([], frame=None)`` means the inferior is running, so the
      current tree stays visible.
    - ``set_variables([], frame=<frame>)`` means the inferior is stopped in a
      frame that simply has no locals, so the tree is reconciled to empty.

    Callers should treat every method not described above as an internal
    implementation detail of the package.
    """

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

    _RE_CONTAINER_LENGTH = re.compile(r"(?:length|size)\s+(\d+)|with\s+(\d+)\s+elements", re.IGNORECASE)
    _SAFE_CHILD_COUNT = 1_000_000

    def __init__(self, hl: HighlightGroups, cfg: Config, **kwargs) -> None:
        """Create a locals pane widget.

        Args:
            hl: Highlight-group palette shared by tgdb panes.
            cfg: Runtime configuration. ``LocalVariablePane`` currently uses it
                for behaviors such as ``expandchildlimit``.
            **kwargs: Forwarded to ``PaneBase`` / Textual widget construction.

        The pane is intentionally inert after construction. It becomes fully
        operational once ``set_var_callbacks`` has been called.
        """
        super().__init__(hl, **kwargs)
        self._cfg = cfg
        self._variables: list[LocalVariable] = []

        self._var_create: Optional[Callable[..., Coroutine]] = None
        self._var_list_children: Optional[Callable[..., Coroutine]] = None
        self._var_delete: Optional[Callable[..., Coroutine]] = None
        self._var_update: Optional[Callable[..., Coroutine]] = None
        self._var_eval: Optional[Callable[..., Coroutine]] = None
        self._var_eval_expr: Optional[Callable[..., Coroutine]] = None
        self._get_decl_lines: Optional[Callable[..., Coroutine]] = None

        self._tracked: dict[tuple[str, str], str] = {}
        self._pinned_varobjs: set[str] = set()
        self._varobj_type: dict[str, str] = {}
        self._varobj_to_node: dict[str, TreeNode] = {}
        self._varobj_names: list[str] = []
        self._dynamic_varobjs: set[str] = set()
        self._uninitialized_nodes: dict[tuple[str, str], TreeNode] = {}

        self._frame_key: tuple | None = None
        self._saved_expansions: dict[tuple, set[tuple[tuple[str, int], ...]]] = {}
        self._rebuild_gen = 0


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
        """Install the async debugger callbacks used by the pane.

        This is the only required dependency-injection step after
        construction. Once these callbacks are installed, the pane can be used
        as a black-box: callers only need to push fresh locals snapshots
        through ``set_variables``.

        The callbacks are expected to follow the contract documented in the
        class docstring. In the tgdb app, they are normally wired directly to
        ``GDBController`` / ``VarobjMixin`` methods.
        """
        self._var_create = var_create
        self._var_list_children = var_list_children
        self._var_delete = var_delete
        self._var_update = var_update
        self._var_eval = var_eval
        self._var_eval_expr = var_eval_expr
        self._get_decl_lines = get_decl_lines


    def set_variables(self, variables: list[LocalVariable], frame: Frame | None = None) -> None:
        """Publish the latest locals snapshot for the active debugger frame.

        This is the main caller-facing mutation API.

        Args:
            variables: The locals currently reported by the debugger for the
                selected frame.
            frame: The current frame metadata. Pass ``None`` only when the
                inferior is running and there is no meaningful current frame to
                display.

        Semantics:

        - ``set_variables(vars, frame)`` with a real frame triggers an
          incremental reconciliation against the live tree. Existing nodes are
          updated in place where possible so expansion state can be preserved.
        - ``set_variables([], frame=None)`` means "the inferior is running".
          The pane keeps the current tree visible until the next stop, which
          matches cgdb's feel better than clearing immediately.
        - ``set_variables([], frame=<frame>)`` means "the inferior is stopped,
          but this frame has no locals". In that case the tree is updated to
          the empty state.

        The pane internally uses a generation counter so repeated calls are
        safe: newer snapshots automatically supersede older async work.
        """
        self._variables = list(variables)
        self._rebuild_gen += 1
        gen = self._rebuild_gen
        if not variables and frame is None:
            return

        asyncio.create_task(self._update_variables(gen, frame, self._variables))


    @classmethod
    def _parse_container_length(cls, value_str: str) -> int | None:
        """Return the container length from a GDB summary string, or None."""
        if "<error reading" in value_str or "Cannot access memory" in value_str:
            return None

        match = cls._RE_CONTAINER_LENGTH.search(value_str)
        if not match:
            return None

        if match.group(1) is not None:
            return int(match.group(1))

        return int(match.group(2))


    def _child_fetch_limit(self, displayhint: str) -> int:
        """Return the raw GDB child limit for the given pretty-printer hint."""
        limit = self._cfg.expandchildlimit
        if displayhint == "map" and limit > 0:
            return limit * 2

        return limit


    @staticmethod
    def _child_display_count(raw_count: int, displayhint: str) -> int:
        """Convert a raw GDB child count to the user-visible item count."""
        if displayhint == "map":
            return raw_count // 2

        return raw_count
