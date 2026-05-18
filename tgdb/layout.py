"""Layout helpers for the application package.

The legacy cgdb-style ``winsplit`` / ``winsplitorientation`` two-pane
machinery is gone.  Every container — including the root ``#split-container``
— uses the generic nested ``PaneContainer`` split algorithm.

This module now only routes the source-pane keyboard shortcuts that act
on the root container as a convenience:

  =/- : grow/shrink the root's first child by 1 cell along the
        root's orientation axis
  +/_ : grow/shrink the root's first child by ~25% of the root's
        size along the orientation axis
  Ctrl+W : toggle the root container's orientation

All three shortcuts require the root to have at least 2 children
(otherwise there is nothing to resize / nothing distinct to toggle).
"""

from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from .source_widget import ResizeSource, ToggleOrientation
from .workspace import PaneContainer

if TYPE_CHECKING:
    from .main import TGDBApp


class LayoutMixin:
    """Root-container keyboard-shortcut routing."""

    def _get_root_container(self: "TGDBApp") -> PaneContainer | None:
        try:
            return self.query_one("#split-container", PaneContainer)
        except NoMatches:
            return None


    def on_resize_source(self: "TGDBApp", msg: ResizeSource) -> None:
        """Handle ``-``/``=``/``_``/``+`` keys: resize the root's first child.

        Effect on the rest of the children: they shrink / grow
        proportionally to their current sizes so their relative
        ratios stay the same.
        """
        root = self._get_root_container()
        if root is None or len(root.items) < 2:
            return

        is_horizontal = root.orientation == "horizontal"
        if is_horizontal:
            axis = root.size.width
        else:
            axis = root.size.height
        if axis <= 0:
            return

        if msg.jump:
            step = max(1, axis // 4)
            delta = step * (1 if msg.delta > 0 else -1)
        else:
            delta = msg.delta
        root.resize_first_child(delta)


    async def on_toggle_orientation(self: "TGDBApp", _: ToggleOrientation) -> None:
        """Handle Ctrl+W: flip the root container's orientation."""
        root = self._get_root_container()
        if root is None or len(root.items) < 2:
            return
        if root.orientation == "horizontal":
            new_orientation = "vertical"
        else:
            new_orientation = "horizontal"
        await root.set_orientation_async(new_orientation)
