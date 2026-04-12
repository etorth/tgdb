"""File-loading helpers for the internal source-pane content widget."""

from __future__ import annotations

import logging
import os

from ..gdb_controller import Breakpoint
from ..source_data import BP_DISABLED, BP_ENABLED, BP_NONE, SourceFile

_log = logging.getLogger("tgdb.source")


class SourceFileMixin:
    """Mixin providing file and breakpoint state updates for ``_SourceContent``."""

    def load_file(self, path: str) -> bool:
        try:
            previous = self.source_file
            if previous:
                self._file_positions[previous.path] = self.sel_line

            with open(path, errors="replace") as handle:
                content = handle.read()
            lines = content.expandtabs(self.tabstop).splitlines()
            if not lines:
                lines = [""]
            source_file = SourceFile(path, lines)
            if previous and previous.path == path:
                source_file.bp_flags = list(previous.bp_flags[: len(lines)])
                while len(source_file.bp_flags) < len(lines):
                    source_file.bp_flags.append(BP_NONE)
                source_file.marks_local = dict(previous.marks_local)
            self.source_file = source_file
            self._show_logo = False
            self._col_offset = 0
            target_line = self._file_positions.get(path, 1)
            self.sel_line = max(1, min(target_line, len(lines)))
            self._ensure_visible(self.sel_line)
            self.refresh()
            _log.info(f"load file: {path}")
            return True
        except OSError as exc:
            _log.warning(f"load file failed: {path}: {exc}")
            return False


    def reload_if_changed(self) -> bool:
        source_file = self.source_file
        if not source_file:
            return False
        try:
            mtime = os.path.getmtime(source_file.path)
            if mtime != source_file.mtime:
                return self.load_file(source_file.path)
        except OSError:
            pass
        return False


    def set_breakpoints(self, bps: list[Breakpoint]) -> None:
        source_file = self.source_file
        if not source_file:
            return
        source_file.bp_flags = [BP_NONE] * len(source_file.lines)
        for breakpoint_info in bps:
            fullname = breakpoint_info.fullname or breakpoint_info.file
            if not fullname:
                continue
            try:
                same_file = (
                    os.path.abspath(fullname) == os.path.abspath(source_file.path)
                    or os.path.basename(fullname) == os.path.basename(source_file.path)
                )
            except Exception:
                same_file = False
            if same_file and 1 <= breakpoint_info.line <= len(source_file.lines):
                if breakpoint_info.enabled:
                    source_file.bp_flags[breakpoint_info.line - 1] = BP_ENABLED
                else:
                    source_file.bp_flags[breakpoint_info.line - 1] = BP_DISABLED
        self.refresh()
