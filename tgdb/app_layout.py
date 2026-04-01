"""LayoutMixin — split / resize management extracted from TGDBApp."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.css.query import NoMatches

from .source_widget import ResizeSource, ToggleOrientation
from .workspace import DragResize, PaneContainer, Splitter

if TYPE_CHECKING:
    from .app import TGDBApp


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
    _SPLIT_NAMES = {value: key for key, value in _SPLIT_MARKS.items()}

    def _split_axis(self: TGDBApp, is_horizontal: bool) -> int:
        try:
            container = self.query_one("#split-container")
            axis = container.size.width if is_horizontal else container.size.height
            if axis:
                return max(1, axis)
        except NoMatches:
            pass
        return max(1, self.size.width if is_horizontal else max(1, self.size.height - 1))

    def _pane_axis(self: TGDBApp, is_horizontal: bool) -> int:
        return max(0, self._split_axis(is_horizontal) - 1)

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
        min_size = self.cfg.winminwidth if is_horizontal else self.cfg.winminheight
        min_shift = min_size - base
        max_shift = (axis - min_size) - base

        if max_shift < min_shift:
            max_shift = min_shift = 0

        if self._window_shift > max_shift:
            self._window_shift = max_shift
        elif self._window_shift < min_shift:
            self._window_shift = min_shift

    def _compute_split_sizes(self: TGDBApp, is_horizontal: bool, axis: int | None = None) -> tuple[int, int]:
        axis = self._pane_axis(is_horizontal) if axis is None else max(0, axis)
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
        if self._workspace_dynamic:
            return
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
                if self._cur_win_split == self._WIN_SPLIT_FREE and self._window_shift > 0:
                    split += 1
                elif self._cur_win_split != self._WIN_SPLIT_FREE:
                    split += 1
                split = min(2, split)
            else:
                if self._cur_win_split == self._WIN_SPLIT_FREE and self._window_shift < 0:
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
        new_orientation = "vertical" if self.cfg.winsplitorientation == "horizontal" else "horizontal"
        self.cfg.winsplitorientation = new_orientation
        if self._workspace_dynamic:
            try:
                self.query_one("#split-container", PaneContainer).set_orientation(new_orientation)
            except NoMatches:
                pass
            return
        self._set_window_shift_from_ratio(new_orientation == "horizontal", self._split_ratio)
        self._preserve_window_shift_once = True
        self._apply_split()

    # ------------------------------------------------------------------
    # Apply split
    # ------------------------------------------------------------------

    def _apply_split(self: TGDBApp) -> None:
        if self._workspace_dynamic:
            try:
                self.query_one("#split-container", PaneContainer).set_orientation(self.cfg.winsplitorientation)
            except (NoMatches, Exception):
                # WrongType or missing: dynamic workspace is in transition, skip
                pass
            self._last_orientation = self.cfg.winsplitorientation
            return
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
        show_splitter = src_size > 0 and gdb_size > 0
        if not show_splitter:
            src_size, gdb_size = self._compute_split_sizes(is_horizontal, total_axis)
        pane_total = max(1, src_size + gdb_size)
        self._split_ratio = src_size / pane_total

        try:
            container = self.query_one("#split-container")
            splitter = self.query_one("#splitter", Splitter)
            src = self._get_source_view(mounted_only=True)
            gdb = self._get_gdb_widget(mounted_only=True)
            if src is None or gdb is None:
                return
            splitter.set_orientation(is_horizontal)
            splitter.styles.display = "block" if show_splitter else "none"
            if is_horizontal:
                # Horizontal container: source on the left, GDB on the right.
                container.styles.layout = "horizontal"
                src.styles.display = "none" if src_size <= 0 else "block"
                gdb.styles.display = "none" if gdb_size <= 0 else "block"
                src.styles.width = src_size
                src.styles.height = "1fr"
                gdb.styles.width = gdb_size
                gdb.styles.height = "1fr"
            else:
                # Vertical container: source on top, GDB below.
                container.styles.layout = "vertical"
                src.styles.display = "none" if src_size <= 0 else "block"
                gdb.styles.display = "none" if gdb_size <= 0 else "block"
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
        if self._workspace_dynamic:
            return
        is_horizontal = self.cfg.winsplitorientation == "horizontal"
        axis = self._pane_axis(is_horizontal)
        if axis <= 0:
            return

        try:
            container = self.query_one("#split-container")
            origin_x = getattr(container.region, "x", 0)
            origin_y = getattr(container.region, "y", 0)
        except NoMatches:
            origin_x = 0
            origin_y = 0

        pos = (msg.screen_x - origin_x) if is_horizontal else (msg.screen_y - origin_y)
        pos = max(0, min(axis, int(pos)))
        self._cur_win_split = self._WIN_SPLIT_FREE
        self._window_shift = pos - (axis // 2)
        self._validate_window_shift(is_horizontal)
        self.cfg.winsplit = "free"
        self._apply_split()

    def on_resize(self: TGDBApp, event: events.Resize) -> None:
        if self._workspace_dynamic:
            try:
                self.query_one("#split-container", PaneContainer).refresh(layout=True)
            except NoMatches:
                pass
            return
        self._apply_split()
        # GDBWidget.on_resize handles pyte + PTY resize itself via resize_gdb callback
