"""Layout helpers for the application package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.css.query import NoMatches

from ..source_widget import ResizeSource, ToggleOrientation
from .workspace import DragResize, PaneContainer, TitleBarResized

if TYPE_CHECKING:
    from .main import TGDBApp


class LayoutMixin:
    """Split / resize management."""

    _WIN_SPLIT_FREE = -3
    _SPLIT_MARKS = {
        "gdb_full": -2,
        "gdb_big": -1,
        "even": 0,
        "src_big": 1,
        "src_full": 2,
    }
    # Reverse lookup: split value → name (built from _SPLIT_MARKS below)
    _SPLIT_NAMES = {
        -2: "gdb_full",
        -1: "gdb_big",
        0: "even",
        1: "src_big",
        2: "src_full",
    }

    def _split_axis(self: TGDBApp, is_horizontal: bool) -> int:
        try:
            container = self.query_one("#split-container")
            if is_horizontal:
                axis = container.size.width
            else:
                axis = container.size.height
            if axis:
                return max(1, axis)
        except NoMatches:
            pass
        if is_horizontal:
            fallback = self.size.width
        else:
            fallback = max(1, self.size.height - 1)
        return max(1, fallback)


    def _pane_axis(self: TGDBApp, is_horizontal: bool) -> int:
        # Subtract 1 only for horizontal mode where a 1-column Splitter exists.
        if is_horizontal:
            splitter_size = 1
        else:
            splitter_size = 0
        return max(0, self._split_axis(is_horizontal) - splitter_size)


    def _reset_window_shift(self: TGDBApp, is_horizontal: bool) -> None:
        half_axis = self._pane_axis(is_horizontal) // 2
        self._window_shift = int(half_axis * (self._cur_win_split / 2.0))
        self._validate_window_shift(is_horizontal)


    def _set_window_shift_from_ratio(self: TGDBApp, is_horizontal: bool, ratio: float) -> None:
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            self._window_shift = 0
            return
        target_src = int(round(axis * ratio))
        self._window_shift = target_src - (axis // 2)
        self._validate_window_shift(is_horizontal)


    def _validate_window_shift(self: TGDBApp, is_horizontal: bool) -> None:
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            self._window_shift = 0
            return
        base = axis // 2
        if is_horizontal:
            min_size = self.cfg.winminwidth
        else:
            min_size = self.cfg.winminheight
        min_shift = min_size - base
        max_shift = (axis - min_size) - base

        if max_shift < min_shift:
            max_shift = min_shift = 0

        if self._window_shift > max_shift:
            self._window_shift = max_shift
        elif self._window_shift < min_shift:
            self._window_shift = min_shift


    def _compute_split_sizes(self: TGDBApp, is_horizontal: bool, axis: int | None = None) -> tuple[int, int]:
        if axis is None:
            axis = self._pane_axis(is_horizontal)
        else:
            axis = max(0, axis)
        if self._cur_win_split == -2:
            return 0, axis
        if self._cur_win_split == 2:
            return axis, 0
        src_size = (axis // 2) + self._window_shift
        src_size = max(0, min(axis, src_size))
        gdb_size = max(0, axis - src_size)
        return src_size, gdb_size

    # ------------------------------------------------------------------
    # Resize / orientation message handlers
    # ------------------------------------------------------------------

    def on_resize_source(self: TGDBApp, msg: ResizeSource) -> None:
        is_horizontal = self.cfg.winsplitorientation == "horizontal"
        half_axis = self._split_axis(is_horizontal) // 2

        if msg.rows:
            # cgdb '=' / '-': change window_shift by exactly 1 unit
            self._cur_win_split = self._WIN_SPLIT_FREE
            self._window_shift += msg.delta
            self._validate_window_shift(is_horizontal)
            self.cfg.winsplit = "free"
            self._apply_split()

        elif msg.jump:
            # cgdb '+' / '_': jump to the next quarter-mark split.
            split = self._cur_win_split
            if split == self._WIN_SPLIT_FREE and half_axis > 0:
                split = int((2 * self._window_shift) / half_axis)

            if msg.delta > 0:
                if (
                    self._cur_win_split == self._WIN_SPLIT_FREE
                    and self._window_shift > 0
                ):
                    split += 1
                elif self._cur_win_split != self._WIN_SPLIT_FREE:
                    split += 1
                split = min(2, split)
            else:
                if (
                    self._cur_win_split == self._WIN_SPLIT_FREE
                    and self._window_shift < 0
                ):
                    split -= 1
                elif self._cur_win_split != self._WIN_SPLIT_FREE:
                    split -= 1
                split = max(-2, split)

            self._cur_win_split = split
            self._window_shift = int(half_axis * (split / 2.0))
            self._validate_window_shift(is_horizontal)
            self.cfg.winsplit = self._SPLIT_NAMES[split]
            self._apply_split()

        else:
            # legacy percent mode
            axis = self._split_axis(is_horizontal)
            self._cur_win_split = self._WIN_SPLIT_FREE
            self._window_shift += int((axis * msg.delta) / 100)
            self._validate_window_shift(is_horizontal)
            self.cfg.winsplit = "free"
            self._apply_split()


    def on_toggle_orientation(self: TGDBApp, _: ToggleOrientation) -> None:
        if self.cfg.winsplitorientation == "horizontal":
            new_orientation = "vertical"
        else:
            new_orientation = "horizontal"
        self.cfg.winsplitorientation = new_orientation
        self._set_window_shift_from_ratio(
            new_orientation == "horizontal", self._split_ratio
        )
        self._preserve_window_shift_once = True
        self._apply_split()

    # ------------------------------------------------------------------
    # Apply split
    # ------------------------------------------------------------------

    def _apply_split(self: TGDBApp) -> None:
        split = self.cfg.winsplit.lower()
        is_horizontal = self.cfg.winsplitorientation == "horizontal"
        split_changed = split != self._last_split_setting
        orientation_changed = self.cfg.winsplitorientation != self._last_orientation

        if split in self._SPLIT_MARKS:
            self._cur_win_split = self._SPLIT_MARKS[split]
            if split_changed:
                self._reset_window_shift(is_horizontal)
            elif orientation_changed and not self._preserve_window_shift_once:
                self._set_window_shift_from_ratio(is_horizontal, self._split_ratio)
        elif orientation_changed and not self._preserve_window_shift_once:
            self._set_window_shift_from_ratio(is_horizontal, self._split_ratio)

        self._validate_window_shift(is_horizontal)
        total_axis = max(1, self._split_axis(is_horizontal))
        src_size, gdb_size = self._compute_split_sizes(is_horizontal)
        show_both = src_size > 0 and gdb_size > 0
        if not show_both:
            src_size, gdb_size = self._compute_split_sizes(is_horizontal, total_axis)
        pane_total = max(1, src_size + gdb_size)
        self._split_ratio = src_size / pane_total

        try:
            container = self.query_one("#split-container", PaneContainer)
            src = self._get_source_view(mounted_only=True)
            gdb = self._get_gdb_widget(mounted_only=True)
            if src is None or gdb is None:
                return
            if orientation_changed:
                container.set_orientation(self.cfg.winsplitorientation)
            if is_horizontal:
                container.styles.layout = "horizontal"
                if src_size <= 0:
                    src.styles.display = "none"
                else:
                    src.styles.display = "block"
                if gdb_size <= 0:
                    gdb.styles.display = "none"
                else:
                    gdb.styles.display = "block"
                src.styles.width = src_size
                src.styles.height = "1fr"
                gdb.styles.width = gdb_size
                gdb.styles.height = "1fr"
            else:
                container.styles.layout = "vertical"
                if src_size <= 0:
                    src.styles.display = "none"
                else:
                    src.styles.display = "block"
                if gdb_size <= 0:
                    gdb.styles.display = "none"
                else:
                    gdb.styles.display = "block"
                src.styles.width = "1fr"
                src.styles.height = src_size
                gdb.styles.width = "1fr"
                gdb.styles.height = gdb_size
        except NoMatches:
            pass
        finally:
            self._last_split_setting = split
            self._last_orientation = self.cfg.winsplitorientation
            self._preserve_window_shift_once = False


    def on_drag_resize(self: TGDBApp, msg: DragResize) -> None:
        """Handle Splitter drag for the root #split-container (horizontal mode)."""
        if msg.splitter is None:
            return
        try:
            root = self.query_one("#split-container", PaneContainer)
        except NoMatches:
            return
        if msg.splitter.parent is not root:
            return  # nested PaneContainer already handled it

        is_horizontal = self.cfg.winsplitorientation == "horizontal"
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            return

        origin_x = getattr(root.region, "x", 0)
        origin_y = getattr(root.region, "y", 0)
        if is_horizontal:
            pos = msg.screen_x - origin_x
        else:
            pos = msg.screen_y - origin_y
        pos = max(0, min(axis, int(pos)))
        self._cur_win_split = self._WIN_SPLIT_FREE
        self._window_shift = pos - (axis // 2)
        self._validate_window_shift(is_horizontal)
        self.cfg.winsplit = "free"
        self._apply_split()


    def on_title_bar_resized(self: TGDBApp, msg: TitleBarResized) -> None:
        """Sync _window_shift after a vertical title-bar drag on the root container."""
        try:
            root = self.query_one("#split-container", PaneContainer)
        except NoMatches:
            return
        if msg.container is not root:
            return
        is_horizontal = self.cfg.winsplitorientation == "horizontal"
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            return
        self._cur_win_split = self._WIN_SPLIT_FREE
        self._window_shift = msg.new_before_size - (axis // 2)
        self.cfg.winsplit = "free"
        self._validate_window_shift(is_horizontal)
        # Apply absolute pixel heights (same as -/= keys) so Textual fires
        # proper Resize events on SourceView and _SourceContent.  Without
        # this, _apply_orientation() leaves fractional "fr" heights that
        # don't reliably trigger Resize, leaving blank rows in the source pane.
        self._apply_split()


    def on_resize(self: TGDBApp, event: events.Resize) -> None:
        self._apply_split()
        # GDBWidget.on_resize handles pyte + PTY resize itself via resize_gdb callback
